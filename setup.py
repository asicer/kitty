#!/usr/bin/env python3
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

import argparse
import glob
import importlib
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import sysconfig
import time
import queue
import pty
import struct
import fcntl
import termios
import asyncio
import signal
from kitty.enums import BuildType

base = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(base, 'glfw'))
glfw = importlib.import_module('glfw')
verbose = False
del sys.path[0]
build_dir = os.path.join(base, 'build')
constants = os.path.join(base, 'kitty', 'constants.py')
with open(constants, 'rb') as f:
    constants = f.read().decode('utf-8')
appname = re.search(r"^appname = '([^']+)'", constants, re.MULTILINE).group(1)
version = tuple(
    map(
        int,
        re.search(
            r"^version = \((\d+), (\d+), (\d+)\)", constants, re.MULTILINE
        ).group(1, 2, 3)
    )
)
_plat = sys.platform.lower()
is_macos = 'darwin' in _plat
env = None

PKGCONFIG = os.environ.get('PKGCONFIG_EXE', 'pkg-config')


def emphasis(text):
    if sys.stdout.isatty():
        text = '\033[32m' + text + '\033[39m'
    return text


def error(text):
    if sys.stdout.isatty():
        text = '\033[91m' + text + '\033[39m'
    return text


def pkg_config(pkg, *args):
    try:
        return list(
            filter(
                None,
                shlex.split(
                    subprocess.check_output([PKGCONFIG, pkg] + list(args))
                    .decode('utf-8')
                )
            )
        )
    except subprocess.CalledProcessError:
        raise SystemExit('The package {} was not found on your system'.format(error(pkg)))


def at_least_version(package, major, minor=0):
    q = '{}.{}'.format(major, minor)
    if subprocess.run([PKGCONFIG, package, '--atleast-version=' + q]
                      ).returncode != 0:
        try:
            ver = subprocess.check_output([PKGCONFIG, package, '--modversion']
                                          ).decode('utf-8').strip()
            qmajor, qminor = map(int, re.match(r'(\d+).(\d+)', ver).groups())
        except Exception:
            ver = 'not found'
            qmajor = qminor = 0
        if qmajor < major or (qmajor == major and qminor < minor):
            raise SystemExit(
                '{} >= {}.{} is required, found version: {}'.format(
                    error(package), major, minor, ver
                )
            )


def cc_version():
    if 'CC' in os.environ:
        cc = os.environ['CC']
    else:
        if is_macos:
            cc = 'clang'
        else:
            if shutil.which('gcc'):
                cc = 'gcc'
            elif shutil.which('clang'):
                cc = 'clang'
            else:
                cc = 'cc'
    raw = subprocess.check_output([cc, '-dumpversion']).decode('utf-8')
    ver = raw.split('.')[:2]
    try:
        ver = tuple(map(int, ver))
    except Exception:
        ver = (0, 0)
    return cc, ver


def get_python_flags(cflags):
    cflags.extend(
        '-I' + sysconfig.get_path(x) for x in 'include platinclude'.split()
    )
    libs = []
    libs += sysconfig.get_config_var('LIBS').split()
    libs += sysconfig.get_config_var('SYSLIBS').split()
    fw = sysconfig.get_config_var('PYTHONFRAMEWORK')
    if fw:
        for var in 'data include stdlib'.split():
            val = sysconfig.get_path(var)
            if val and '/{}.framework'.format(fw) in val:
                fdir = val[:val.index('/{}.framework'.format(fw))]
                if os.path.isdir(
                    os.path.join(fdir, '{}.framework'.format(fw))
                ):
                    framework_dir = fdir
                    break
        else:
            raise SystemExit('Failed to find Python framework')
        libs.append(
            os.path.join(framework_dir, sysconfig.get_config_var('LDLIBRARY'))
        )
    else:
        libs += ['-L' + sysconfig.get_config_var('LIBDIR')]
        libs += [
            '-lpython' + sysconfig.get_config_var('VERSION') + sys.abiflags
        ]
        libs += sysconfig.get_config_var('LINKFORSHARED').split()
    return libs


def get_sanitize_args(cc, ccver):
    sanitize_args = ['-fsanitize=address']
    if ccver >= (5, 0):
        sanitize_args.append('-fsanitize=undefined')
        # if cc == 'gcc' or (cc == 'clang' and ccver >= (4, 2)):
        #     sanitize_args.append('-fno-sanitize-recover=all')
    sanitize_args.append('-fno-omit-frame-pointer')
    return sanitize_args


def test_compile(cc, *cflags, src=None):
    src = src or 'int main(void) { return 0; }'
    p = subprocess.Popen([cc] + list(cflags) + ['-x', 'c', '-o', os.devnull, '-'], stdin=subprocess.PIPE)
    try:
        p.stdin.write(src.encode('utf-8')), p.stdin.close()
    except BrokenPipeError:
        return False
    return p.wait() == 0


def first_successful_compile(cc, *cflags, src=None):
    for x in cflags:
        if test_compile(cc, *shlex.split(x), src=src):
            return x
    return ''


class Env:

    def __init__(self, cc, cppflags, cflags, ldflags, ldpaths=[]):
        self.cc, self.cppflags, self.cflags, self.ldflags, self.ldpaths = cc, cppflags, cflags, ldflags, ldpaths

    def copy(self):
        return Env(self.cc, list(self.cppflags), list(self.cflags), list(self.ldflags), list(self.ldpaths))


def init_env(
    debug=False, sanitize=False, native_optimizations=True, profile=False,
    extra_logging=()
):
    native_optimizations = native_optimizations and not sanitize and not debug
    cc, ccver = cc_version()
    print('CC:', cc, ccver)
    stack_protector = first_successful_compile(cc, '-fstack-protector-strong', '-fstack-protector')
    missing_braces = ''
    if ccver < (5, 2) and cc == 'gcc':
        missing_braces = '-Wno-missing-braces'
    df = '-g3'
    if ccver >= (5, 0):
        df += ' -Og'
    optimize = df if debug or sanitize else '-O3'
    sanitize_args = get_sanitize_args(cc, ccver) if sanitize else set()
    cppflags = os.environ.get(
        'OVERRIDE_CPPFLAGS', (
            '-D{}DEBUG'
        ).format(
            ('' if debug else 'N'),
        )
    )
    cppflags = shlex.split(cppflags)
    for el in extra_logging:
        cppflags.append('-DDEBUG_{}'.format(el.upper().replace('-', '_')))
    cflags = os.environ.get(
        'OVERRIDE_CFLAGS', (
            '-Wextra -Wno-missing-field-initializers -Wall -Wstrict-prototypes -std=c11'
            ' -pedantic-errors -Werror {} {} -fwrapv {} {} -pipe {} -fvisibility=hidden'
        ).format(
            optimize,
            ' '.join(sanitize_args),
            stack_protector,
            missing_braces,
            '-march=native' if native_optimizations else '',
        )
    )
    cflags = shlex.split(cflags) + shlex.split(
        sysconfig.get_config_var('CCSHARED')
    )
    ldflags = os.environ.get(
        'OVERRIDE_LDFLAGS',
        '-Wall ' + ' '.join(sanitize_args) + ('' if debug else ' -O3')
    )
    ldflags = shlex.split(ldflags)
    ldflags.append('-shared')
    cppflags += shlex.split(os.environ.get('CPPFLAGS', ''))
    cflags += shlex.split(os.environ.get('CFLAGS', ''))
    ldflags += shlex.split(os.environ.get('LDFLAGS', ''))
    if not debug and not sanitize:
        # See https://github.com/google/sanitizers/issues/647
        cflags.append('-flto'), ldflags.append('-flto')

    if profile:
        cppflags.append('-DWITH_PROFILER')
        cflags.append('-g3')
        ldflags.append('-lprofiler')
    ldpaths = []
    return Env(cc, cppflags, cflags, ldflags, ldpaths=ldpaths)


def kitty_env():
    ans = env.copy()
    cflags = ans.cflags
    cflags.append('-pthread')
    # We add 4000 to the primary version because vim turns on SGR mouse mode
    # automatically if this version is high enough
    cppflags = ans.cppflags
    cppflags.append('-DPRIMARY_VERSION={}'.format(version[0] + 4000))
    cppflags.append('-DSECONDARY_VERSION={}'.format(version[1]))
    at_least_version('harfbuzz', 1, 5)
    cflags.extend(pkg_config('libpng', '--cflags-only-I'))
    if is_macos:
        font_libs = ['-framework', 'CoreText', '-framework', 'CoreGraphics']
        # Apple deprecated OpenGL in Mojave (10.14) silence the endless
        # warnings about it
        cppflags.append('-DGL_SILENCE_DEPRECATION')
    else:
        cflags.extend(pkg_config('fontconfig', '--cflags-only-I'))
        font_libs = pkg_config('fontconfig', '--libs')
    cflags.extend(pkg_config('harfbuzz', '--cflags-only-I'))
    font_libs.extend(pkg_config('harfbuzz', '--libs'))
    pylib = get_python_flags(cflags)
    gl_libs = ['-framework', 'OpenGL'] if is_macos else pkg_config('gl', '--libs')
    libpng = pkg_config('libpng', '--libs')
    ans.ldpaths += pylib + font_libs + gl_libs + libpng
    if is_macos:
        ans.ldpaths.extend('-framework Cocoa'.split())
    else:
        ans.ldpaths += ['-lrt']
        if '-ldl' not in ans.ldpaths:
            ans.ldpaths.append('-ldl')
    if '-lz' not in ans.ldpaths:
        ans.ldpaths.append('-lz')

    try:
        os.mkdir(build_dir)
    except FileExistsError:
        pass
    return ans


def define(x):
    return '-D' + x


def run_tool(cmd, desc=None):
    if isinstance(cmd, str):
        cmd = shlex.split(cmd[0])
    if verbose:
        desc = None
    print(desc or ' '.join(cmd))
    p = subprocess.Popen(cmd)
    ret = p.wait()
    if ret != 0:
        if desc:
            print(' '.join(cmd))
        raise SystemExit(ret)


SPECIAL_SOURCES = {
    'kitty/parser_dump.c': ('kitty/parser.c', ['DUMP_COMMANDS']),
}
if os.path.exists('.git'):
    rev = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode('utf-8').strip()
    SPECIAL_SOURCES.update({'kitty/data-types.c': ('kitty/data-types.c', ['KITTY_VCS_REV="{}"'.format(rev)])})


def newer(dest, *sources):
    try:
        dtime = os.path.getmtime(dest)
    except EnvironmentError:
        return True
    for s in sources:
        if os.path.getmtime(s) >= dtime:
            return True
    return False


def dependecies_for(src, obj, all_headers):
    dep_file = obj.rpartition('.')[0] + '.d'
    try:
        deps = open(dep_file).read()
    except FileNotFoundError:
        yield src
        yield from iter(all_headers)
    else:
        RE_INC = re.compile(
            r'^(?P<target>.+?):\s+(?P<deps>.+?)$', re.MULTILINE
        )
        SPACE_TOK = '\x1B'

        text = deps.replace('\\\n', ' ').replace('\\ ', SPACE_TOK)
        for match in RE_INC.finditer(text):
            files = (
                f.replace(SPACE_TOK, ' ') for f in match.group('deps').split()
            )
            for path in files:
                path = os.path.abspath(path)
                if path.startswith(base):
                    yield path


def prepare_compile_c_extension(kenv, module, incremental, old_compilation_database, compilation_database, sources, headers, src_deps=None):
    to_compile = {}
    deps = []
    objects = []
    new_objects = []

    for src in sources:
        name = src
        cppflags = kenv.cppflags[:]
        prefix = os.path.basename(module)
        dest = os.path.join(build_dir, prefix + '-' + os.path.basename(src) + '.o')
        is_special = src in SPECIAL_SOURCES
        if is_special:
            src, defines = SPECIAL_SOURCES[src]
            cppflags.extend(map(define, defines))
        cmd = [kenv.cc, '-MMD'] + cppflags + kenv.cflags
        full_src = os.path.join(base, src)
        compilation_key = name, module
        old_cmd = old_compilation_database.get(compilation_key, [])
        if old_cmd is not None:
            cmd_changed = old_cmd[:-4] != cmd
        else:
            cmd_changed = True
        if not incremental or cmd_changed or newer(
            dest, *dependecies_for(full_src, dest, headers)
        ):
            new_objects += [dest]
            done = False
        else:
            done = True
        cmd += ['-c', full_src] + ['-o', dest]
        compilation_database[compilation_key] = cmd
        to_compile[compilation_key] = [cmd, BuildType.compile, done, done, src_deps, compilation_key, None, None]
        deps += [compilation_key]
        objects += [dest]
    dest = os.path.join(base, module + '.temp.so')
    real_dest = dest[:-len('.temp.so')] + '.so'
    if not incremental or new_objects or newer(real_dest, *objects):
        # Old versions of clang don't like -pthread being passed to the linker
        # Don't treat linker warnings as errors (linker generates spurious
        # warnings on some old systems)
        unsafe = {'-pthread', '-Werror', '-pedantic-errors'}
        linker_cflags = list(filter(lambda x: x not in unsafe, kenv.cflags))
        cmd = [kenv.cc] + linker_cflags + kenv.ldflags + objects + kenv.ldpaths + ['-o', dest]
        to_compile[module, module] = [cmd, BuildType.link, False, False, deps, None, dest, real_dest]
    return to_compile


def fast_compile(to_compile, compilation_database):
    try:
        num_workers = max(1, os.cpu_count())
    except Exception:
        num_workers = 1
    # num_workers += 1
    items = queue.Queue()
    workers = {}
    pid_to_workers = {}
    failed_ret = 0

    def child_exited():
        nonlocal failed_ret
        nonlocal loop
        nonlocal compilation_database
        nonlocal to_compile
        # try:
        pid, status = os.waitpid(-1, os.WNOHANG)
        # except ChildProcessError:  # No child process available for some reason
        #     return
        master = pid_to_workers.pop(pid, None)
        name, module, cmd, w, stderrfd, dest, real_dest = workers.pop(master, (None, None, None, None, None, None, None))
        compilation_key = name, module
        signal_number = status & 0xff
        exit_status = (status >> 8) & 0xff
        if signal_number != 0 or exit_status != 0:
            failed_ret = exit_status
            compilation_database.pop(compilation_key, None)
            if dest is not None:
                try:
                    os.remove(dest)
                except EnvironmentError:
                    pass
        else:
            if dest is not None and real_dest is not None:
                os.rename(dest, real_dest)
        to_compile[compilation_key][3] = True
        loop.stop()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGCHLD, child_exited)

    def ready_to_read(master):
        nonlocal failed_ret
        nonlocal loop
        name, module, cmd, w, stderrfd, dest, real_dest = workers.get(master, (None, None, None, None, None, None, None))
        compilation_key = name, module
        try:
            data = os.read(stderrfd, 1024) # read available
        except OSError as e:
            if e.errno != errno.EIO:
                raise #XXX cleanup
            del readable[fd] # EIO means EOF on some systems
        else:
            if not data: # EOF
                del readable[fd]
            else:
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()
        if not failed_ret:
            failed_ret = 1000

            for key in workers:  # Stop all other workers
                if key == master:
                    continue  # Don't kill this one process
                del workers[key]
                w_name, w_module, _, w, w_master, w_dest, _ = workers[key]
                w.kill()
                w_compilation_key = w_name, w_module
                compilation_database.pop(w_compilation_key, None)
                if w_dest is not None:
                    try:
                        os.remove(w_dest)
                    except EnvironmentError:
                        pass
        loop.stop()

    def wait():
        if not workers:
            return
        loop.run_forever()

    while not failed_ret:
        all_done = True
        for key in to_compile:
            name, module = key
            value = to_compile[key]
            cmd = value[0]
            action = value[1]
            started = value[2]
            done = value[3]
            deps = value[4]
            dest = value[6]
            real_dest = value[7]
            if started or done:
                continue
            all_done = False

            all_deps_done = True
            if deps is not None:
                for dep in deps:
                    if not to_compile[dep][3]:
                        all_deps_done = False
                        break
            if all_deps_done:
                items.put((name, module, cmd, action, dest, real_dest))
                value[2] = True

        while len(workers) < num_workers and not items.empty():
            name, module, cmd, action, dest, real_dest = items.get()
            if verbose:
                print(' '.join(cmd))
            else:
                if action == BuildType.compile:
                    print('Compiling  {} ...'.format(emphasis(name)))
                elif action == BuildType.link:
                    print('Linking    {} ...'.format(emphasis(name)))
                elif action == BuildType.generate:
                    print('Generating {} ...'.format(emphasis(name)))
                else:
                    raise SystemExit('Programming error, unknown action {}'.format(action))
            master, slave = pty.openpty()  # Create a new pty

            s = struct.pack('HHHH', 0, 0, 0, 0)
            t = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, s)
            fcntl.ioctl(master, termios.TIOCSWINSZ, t)  # Set size of pty

            loop.add_reader(master, ready_to_read, master)

            w = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=slave)
            workers[master] = name, module, cmd, w, master, dest, real_dest
            pid_to_workers[w.pid] = master
        wait()

        if all_done and items.empty():
            break

    while len(workers):
        wait()
    loop.close()
    if failed_ret:
        raise SystemExit(failed_ret)
    assert(items.empty())


def find_c_files():
    ans, headers = [], []
    d = os.path.join(base, 'kitty')
    exclude = {'fontconfig.c', 'freetype.c', 'desktop.c'} if is_macos else {'core_text.m', 'cocoa_window.m', 'macos_process_info.c'}
    for x in os.listdir(d):
        ext = os.path.splitext(x)[1]
        if ext in ('.c', '.m') and os.path.basename(x) not in exclude:
            ans.append(os.path.join('kitty', x))
        elif ext == '.h':
            headers.append(os.path.join('kitty', x))
    ans.sort(
        key=lambda x: os.path.getmtime(os.path.join(base, x)), reverse=True
    )
    ans.append('kitty/parser_dump.c')
    return tuple(ans), tuple(headers)


def prepare_compile_glfw(incremental, old_compilation_database, compilation_database):
    to_compile = {}
    modules = ('cocoa',) if is_macos else ('x11', 'wayland')
    for module in modules:
        try:
            genv = glfw.init_env(env, pkg_config, at_least_version, test_compile, module)
        except SystemExit as err:
            if module != 'wayland':
                raise
            print(err, file=sys.stderr)
            print(error('Disabling building of wayland backend'), file=sys.stderr)
            continue
        sources = [os.path.join('glfw', x) for x in genv.sources]
        all_headers = [os.path.join('glfw', x) for x in genv.all_headers]
        glfw_deps = None
        if module == 'wayland':
            try:
                glfw_deps, wayland_to_compile = glfw.prepare_build_wayland_protocols(genv, emphasis, newer, base, 'glfw', module)
                to_compile.update(wayland_to_compile)
            except SystemExit as err:
                print(err, file=sys.stderr)
                print(error('Disabling building of wayland backend'), file=sys.stderr)
                continue
        to_compile.update(
            prepare_compile_c_extension(
                genv, 'kitty/glfw-' + module, incremental, old_compilation_database, compilation_database, sources, all_headers, glfw_deps
            )
        )
    return to_compile


def kittens_env():
    kenv = env.copy()
    cflags = kenv.cflags
    cflags.append('-pthread')
    cflags.append('-Ikitty')
    pylib = get_python_flags(cflags)
    kenv.ldpaths += pylib
    return kenv


def prepare_compile_kittens(incremental, old_compilation_database, compilation_database):
    to_compile = {}
    kenv = kittens_env()

    def list_files(q):
        return [os.path.relpath(x, base) for x in glob.glob(q)]

    def files(kitten, output, extra_headers=(), extra_sources=(), filter_sources=None):
        sources = list(filter(filter_sources, list(extra_sources) + list_files(os.path.join('kittens', kitten, '*.c'))))
        headers = list_files(os.path.join('kittens', kitten, '*.h')) + list(extra_headers)
        return (sources, headers, 'kittens/{}/{}'.format(kitten, output))

    for sources, all_headers, dest in (
        files('unicode_input', 'unicode_names'),
        files('diff', 'diff_speedup'),
        files(
            'choose', 'subseq_matcher',
            extra_headers=('kitty/charsets.h',),
            extra_sources=('kitty/charsets.c',),  # TODO: Compile only once
            filter_sources=lambda x: 'windows_compat.c' not in x),
    ):
        to_compile.update(prepare_compile_c_extension(
            kenv, dest, incremental, old_compilation_database, compilation_database, sources, all_headers + ['kitty/data-types.h']))
    return to_compile


def build(args, native_optimizations=True):
    global env
    compilation_database = {}
    try:
        with open('build/compile_commands.json') as f:
            old_compilation_database = json.load(f)
    except FileNotFoundError:
        old_compilation_database = []
    old_compilation_database = {
        (k['file'], k.get('module')): k['arguments'] for k in old_compilation_database
    }
    env = init_env(args.debug, args.sanitize, native_optimizations, args.profile, args.extra_logging)
    try:
        to_compile = prepare_compile_kittens(args.incremental, old_compilation_database, compilation_database)
        to_compile.update(prepare_compile_c_extension(
            kitty_env(), 'kitty/fast_data_types', args.incremental, old_compilation_database, compilation_database, *find_c_files()
        ))
        to_compile.update(prepare_compile_glfw(args.incremental, old_compilation_database, compilation_database))

        fast_compile(to_compile, compilation_database)
    finally:
        compilation_database = [
            {'file': k[0], 'arguments': v, 'directory': base, 'module': k[1]} for k, v in compilation_database.items()
        ]
        with open('build/compile_commands.json', 'w') as f:
            json.dump(compilation_database, f, indent=2, sort_keys=True)


def safe_makedirs(path):
    os.makedirs(path, exist_ok=True)


def build_asan_launcher(args):
    dest = 'asan-launcher'
    src = 'asan-launcher.c'
    if args.incremental and not newer(dest, src):
        return
    cc, ccver = cc_version()
    cflags = '-g3 -Wall -Werror -fpie -std=c99'.split()
    pylib = get_python_flags(cflags)
    sanitize_lib = ['-lasan'] if cc == 'gcc' and not is_macos else []
    cflags.extend(get_sanitize_args(cc, ccver))
    cmd = [cc] + cflags + [src, '-o', dest] + sanitize_lib + pylib
    run_tool(cmd, desc='Creating {} ...'.format(emphasis('asan-launcher')))


def build_linux_launcher(args, launcher_dir='.', for_bundle=False, sh_launcher=False, for_freeze=False):
    cflags = '-Wall -Werror -fpie'.split()
    cppflags = []
    libs = []
    if args.profile:
        cppflags.append('-DWITH_PROFILER'), cflags.append('-g')
        libs.append('-lprofiler')
    else:
        cflags.append('-O3')
    if for_bundle or for_freeze:
        cppflags.append('-DFOR_BUNDLE')
        cppflags.append('-DPYVER="{}"'.format(sysconfig.get_python_version()))
    elif sh_launcher:
        cppflags.append('-DFOR_LAUNCHER')
    cppflags.append('-DLIB_DIR_NAME="{}"'.format(args.libdir_name.strip('/')))
    pylib = get_python_flags(cflags)
    exe = 'kitty-profile' if args.profile else 'kitty'
    cppflags += shlex.split(os.environ.get('CPPFLAGS', ''))
    cflags += shlex.split(os.environ.get('CFLAGS', ''))
    ldflags = shlex.split(os.environ.get('LDFLAGS', ''))
    if for_freeze:
        ldflags += ['-Wl,-rpath,$ORIGIN/../lib']
    cmd = [env.cc] + cppflags + cflags + [
        'linux-launcher.c', '-o',
        os.path.join(launcher_dir, exe)
    ] + ldflags + libs + pylib
    run_tool(cmd)


# Packaging {{{


def copy_man_pages(ddir):
    mandir = os.path.join(ddir, 'share', 'man')
    safe_makedirs(mandir)
    try:
        shutil.rmtree(os.path.join(mandir, 'man1'))
    except FileNotFoundError:
        pass
    src = os.path.join(base, 'docs/_build/man')
    if not os.path.exists(src):
        raise SystemExit('''\
The kitty man page is missing. If you are building from git then run:
make && make docs
(needs the sphinx documentation system to be installed)
''')
    shutil.copytree(src, os.path.join(mandir, 'man1'))


def copy_html_docs(ddir):
    htmldir = os.path.join(ddir, 'share', 'doc', appname, 'html')
    safe_makedirs(os.path.dirname(htmldir))
    try:
        shutil.rmtree(htmldir)
    except FileNotFoundError:
        pass
    src = os.path.join(base, 'docs/_build/html')
    if not os.path.exists(src):
        raise SystemExit('''\
The kitty html docs are missing. If you are building from git then run:
make && make docs
(needs the sphinx documentation system to be installed)
''')
    shutil.copytree(src, htmldir)


def compile_python(base_path):
    import compileall
    import py_compile
    try:
        num_workers = max(1, os.cpu_count())
    except Exception:
        num_workers = 1
    for root, dirs, files in os.walk(base_path):
        for f in files:
            if f.rpartition('.')[-1] in ('pyc', 'pyo'):
                os.remove(os.path.join(root, f))
    for optimize in (0, 1, 2):
        kwargs = dict(ddir='', force=True, optimize=optimize, quiet=1, workers=num_workers)
        if hasattr(py_compile, 'PycInvalidationMode'):
            kwargs['invalidation_mode'] = py_compile.PycInvalidationMode.UNCHECKED_HASH
        compileall.compile_dir(base_path, **kwargs)


def package(args, for_bundle=False, sh_launcher=False):
    ddir = args.prefix
    if for_bundle or sh_launcher:
        args.libdir_name = 'lib'
    libdir = os.path.join(ddir, args.libdir_name.strip('/'), 'kitty')
    if os.path.exists(libdir):
        shutil.rmtree(libdir)
    os.makedirs(os.path.join(libdir, 'logo'))
    build_terminfo = runpy.run_path('build-terminfo', run_name='import_build')
    for x in (libdir, os.path.join(ddir, 'share')):
        odir = os.path.join(x, 'terminfo')
        safe_makedirs(odir)
        build_terminfo['compile_terminfo'](odir)
    shutil.copy2('__main__.py', libdir)
    shutil.copy2('logo/kitty.rgba', os.path.join(libdir, 'logo'))
    shutil.copy2('logo/kitty.png', os.path.join(libdir, 'logo'))
    shutil.copy2('logo/beam-cursor.png', os.path.join(libdir, 'logo'))
    shutil.copy2('logo/beam-cursor@2x.png', os.path.join(libdir, 'logo'))

    def src_ignore(parent, entries):
        return [
            x for x in entries
            if '.' in x and x.rpartition('.')[2] not in
            ('py', 'so', 'glsl')
        ]

    shutil.copytree('kitty', os.path.join(libdir, 'kitty'), ignore=src_ignore)
    shutil.copytree('kittens', os.path.join(libdir, 'kittens'), ignore=src_ignore)
    compile_python(libdir)
    for root, dirs, files in os.walk(libdir):
        for f in files:
            path = os.path.join(root, f)
            os.chmod(path, 0o755 if f.endswith('.so') else 0o644)
    shutil.copy2('kitty/launcher/kitty', os.path.join(libdir, 'kitty', 'launcher'))
    launcher_dir = os.path.join(ddir, 'bin')
    safe_makedirs(launcher_dir)
    build_linux_launcher(args, launcher_dir, for_bundle, sh_launcher, args.for_freeze)
    if not is_macos:  # {{{ linux desktop gunk
        copy_man_pages(ddir)
        copy_html_docs(ddir)
        icdir = os.path.join(ddir, 'share', 'icons', 'hicolor', '256x256', 'apps')
        safe_makedirs(icdir)
        shutil.copy2('logo/kitty.png', icdir)
        deskdir = os.path.join(ddir, 'share', 'applications')
        safe_makedirs(deskdir)
        with open(os.path.join(deskdir, 'kitty.desktop'), 'w') as f:
            f.write(
                '''\
[Desktop Entry]
Version=1.0
Type=Application
Name=kitty
GenericName=Terminal emulator
Comment=A fast, feature full, GPU based terminal emulator
TryExec=kitty
Exec=kitty
Icon=kitty
Categories=System;TerminalEmulator;
'''
            )
    # }}}

    if for_bundle or sh_launcher:  # macOS bundle gunk {{{
        import plistlib
        logo_dir = os.path.abspath(os.path.join('logo', appname + '.iconset'))
        os.chdir(ddir)
        os.mkdir('Contents')
        os.chdir('Contents')
        VERSION = '.'.join(map(str, version))
        pl = dict(
            CFBundleDevelopmentRegion='English',
            CFBundleDisplayName=appname,
            CFBundleName=appname,
            CFBundleIdentifier='net.kovidgoyal.' + appname,
            CFBundleVersion=VERSION,
            CFBundleShortVersionString=VERSION,
            CFBundlePackageType='APPL',
            CFBundleSignature='????',
            CFBundleExecutable=appname,
            LSMinimumSystemVersion='10.12.0',
            LSRequiresNativeExecution=True,
            NSAppleScriptEnabled=False,
            # Needed for dark mode in Mojave when linking against older SDKs
            NSRequiresAquaSystemAppearance='NO',
            NSHumanReadableCopyright=time.strftime(
                'Copyright %Y, Kovid Goyal'),
            CFBundleGetInfoString='kitty, an OpenGL based terminal emulator https://sw.kovidgoyal.net/kitty',
            CFBundleIconFile=appname + '.icns',
            NSHighResolutionCapable=True,
            NSSupportsAutomaticGraphicsSwitching=True,
            LSApplicationCategoryType='public.app-category.utilities',
            LSEnvironment={'KITTY_LAUNCHED_BY_LAUNCH_SERVICES': '1'},
            NSServices=[
                {
                    'NSMenuItem': {'default': 'New ' + appname + ' Tab Here'},
                    'NSMessage': 'openTab',
                    'NSRequiredContext': {'NSTextContent': 'FilePath'},
                    'NSSendTypes': ['NSFilenamesPboardType', 'public.plain-text'],
                },
                {
                    'NSMenuItem': {'default': 'New ' + appname + ' Window Here'},
                    'NSMessage': 'openOSWindow',
                    'NSRequiredContext': {'NSTextContent': 'FilePath'},
                    'NSSendTypes': ['NSFilenamesPboardType', 'public.plain-text'],
                },
            ],
        )
        with open('Info.plist', 'wb') as fp:
            plistlib.dump(pl, fp)
        os.rename('../share', 'Resources')
        os.rename('../bin', 'MacOS')
        os.rename('../lib', 'Frameworks')
        if not os.path.exists(logo_dir):
            raise SystemExit('The kitty logo has not been generated, you need to run logo/make.py')
        cmd = [env.cc] + ['-Wall', '-Werror'] + [
                os.path.join(base, 'symlink-deref.c'), '-o', os.path.join('MacOS', 'kitty-deref-symlink')]
        run_tool(cmd)

        subprocess.check_call([
            'iconutil', '-c', 'icns', logo_dir, '-o',
            os.path.join('Resources', os.path.basename(logo_dir).partition('.')[0] + '.icns')
        ])
    # }}}
# }}}


def clean():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists('.git'):
        for f in subprocess.check_output(
            'git ls-files --others --ignored --exclude-from=.gitignore'.split()
        ).decode('utf-8').splitlines():
            if f.startswith('logo/kitty.iconset') or f.startswith('dev/'):
                continue
            os.unlink(f)
            if os.sep in f and not os.listdir(os.path.dirname(f)):
                os.rmdir(os.path.dirname(f))
        return
    # Not a git checkout, clean manually

    def safe_remove(*entries):
        for x in entries:
            if os.path.exists(x):
                if os.path.isdir(x):
                    shutil.rmtree(x)
                else:
                    os.unlink(x)

    safe_remove('build', 'compile_commands.json', 'linux-package', 'kitty.app')  # TODO: Remove 'compile_commands.json' in a future version
    for root, dirs, files in os.walk('.'):
        remove_dirs = {d for d in dirs if d == '__pycache__'}
        [(shutil.rmtree(os.path.join(root, d)), dirs.remove(d)) for d in remove_dirs]
        for f in files:
            ext = f.rpartition('.')[-1]
            if ext in ('so', 'dylib', 'pyc', 'pyo'):
                os.unlink(os.path.join(root, f))
    for x in glob.glob('glfw/wayland-*-protocol.[ch]'):
        os.unlink(x)


def option_parser():  # {{{
    p = argparse.ArgumentParser()
    p.add_argument(
        'action',
        nargs='?',
        default='build',
        choices='build test linux-package kitty.app macos-bundle osx-bundle clean'.split(),
        help='Action to perform (default is build)'
    )
    p.add_argument(
        '--debug',
        default=False,
        action='store_true',
        help='Build extension modules with debugging symbols'
    )
    p.add_argument(
        '-v', '--verbose',
        default=0,
        action='count',
        help='Be verbose'
    )
    p.add_argument(
        '--sanitize',
        default=False,
        action='store_true',
        help='Turn on sanitization to detect memory access errors and undefined behavior. Note that if you do turn it on,'
        ' a special executable will be built for running the test suite. If you want to run normal kitty'
        ' with sanitization, use LD_PRELOAD=libasan.so (for gcc) and'
        ' LD_PRELOAD=/usr/lib/clang/4.0.0/lib/linux/libclang_rt.asan-x86_64.so (for clang, changing path as appropriate).'
    )
    p.add_argument(
        '--prefix',
        default='./linux-package',
        help='Where to create the linux package'
    )
    p.add_argument(
        '--full',
        dest='incremental',
        default=True,
        action='store_false',
        help='Do a full build, even for unchanged files'
    )
    p.add_argument(
        '--profile',
        default=False,
        action='store_true',
        help='Use the -pg compile flag to add profiling information'
    )
    p.add_argument(
        '--for-freeze',
        default=False,
        action='store_true',
        help='Internal use'
    )
    p.add_argument(
        '--libdir-name',
        default='lib',
        help='The name of the directory inside --prefix in which to store compiled files. Defaults to "lib"'
    )
    p.add_argument(
        '--extra-logging',
        action='append',
        default=[],
        choices=('event-loop',),
        help='Turn on extra logging for debugging in this build. Can be specified multiple times, to turn'
        'on different types of logging.'
    )
    return p
# }}}


def main():
    global verbose
    if sys.version_info < (3, 5):
        raise SystemExit('python >= 3.5 required')
    args = option_parser().parse_args()
    verbose = args.verbose > 0
    args.prefix = os.path.abspath(args.prefix)
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if args.action == 'build':
        build(args)
        if args.sanitize:
            build_asan_launcher(args)
        if args.profile:
            build_linux_launcher(args)
            print('kitty profile executable is', 'kitty-profile')
    elif args.action == 'test':
        os.execlp(
            sys.executable, sys.executable, os.path.join(base, 'test.py')
        )
    elif args.action == 'linux-package':
        build(args, native_optimizations=False)
        if not os.path.exists(os.path.join(base, 'docs/_build/html')):
            run_tool(['make', 'docs'])
        package(args)
    elif args.action in ('macos-bundle', 'osx-bundle'):
        build(args, native_optimizations=False)
        package(args, for_bundle=True)
    elif args.action == 'kitty.app':
        args.prefix = 'kitty.app'
        if os.path.exists(args.prefix):
            shutil.rmtree(args.prefix)
        build(args)
        package(args, for_bundle=False, sh_launcher=True)
        print('kitty.app successfully built!')
    elif args.action == 'clean':
        clean()


if __name__ == '__main__':
    main()
