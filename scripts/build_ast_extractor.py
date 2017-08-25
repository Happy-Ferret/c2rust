#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import errno
import shutil
import signal
import logging
import argparse
import platform

from typing import List, Union

try:
    import plumbum as pb
except ImportError:
    # run `pip install plumbum` or `easy_install plumbum` to fix
    print >> sys.stderr, "error: python package plumbum is not installed."
    quit(errno.ENOENT)

ROOT_DIR = os.path.dirname(os.path.realpath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(ROOT_DIR, os.pardir))
DEPS_DIR = os.path.join(ROOT_DIR, 'dependencies')

CBOR_URL = "https://codeload.github.com/01org/tinycbor/tar.gz/v0.4.1"
CBOR_ARCHIVE = os.path.join(DEPS_DIR, "tinycbor-0.4.1.tar.gz")
CBOR_SRC = os.path.basename(CBOR_ARCHIVE).replace(".tar.gz", "")
CBOR_SRC = os.path.join(DEPS_DIR, CBOR_SRC)
CBOR_PREFIX = os.path.join(DEPS_DIR, "tinycbor")

BEAR_URL = "https://codeload.github.com/rizsotto/Bear/tar.gz/2.3.6"
BEAR_ARCHIVE = os.path.join(DEPS_DIR, "Bear-2.3.6.tar.gz")
BEAR_SRC = os.path.basename(BEAR_ARCHIVE).replace(".tar.gz", "")
BEAR_SRC = os.path.join(DEPS_DIR, BEAR_SRC)
BEAR_PREFIX = os.path.join(DEPS_DIR, "Bear")
BEAR_BIN = os.path.join(BEAR_PREFIX, "bin/bear")

LLVM_SRC = os.path.join(ROOT_DIR, 'llvm.src')
LLVM_BLD = os.path.join(ROOT_DIR, 'llvm.build')
LLVM_BIN = os.path.join(LLVM_BLD, 'bin')
LLVM_PUBKEY = "8F0871F202119294"
LLVM_VER = "4.0.1"
LLVM_ARCHIVE_URLS = """
http://releases.llvm.org/{ver}/llvm-4.0.1.src.tar.xz
http://releases.llvm.org/{ver}/cfe-{ver}.src.tar.xz
http://releases.llvm.org/{ver}/clang-tools-extra-{ver}.src.tar.xz
http://releases.llvm.org/{ver}/clang-tools-extra-{ver}.src.tar.xz
""".split("\n")
LLVM_ARCHIVE_URLS = [s.format(ver=LLVM_VER) for s in LLVM_ARCHIVE_URLS if s]
LLVM_SIGNATURE_URLS = [s + ".sig" for s in LLVM_ARCHIVE_URLS]
LLVM_ARCHIVE_FILES = [os.path.basename(s) for s in LLVM_ARCHIVE_URLS]
LLVM_ARCHIVE_DIRS = [s.replace(".tar.xz", "") for s in LLVM_ARCHIVE_FILES]
LLVM_ARCHIVE_FILES = [os.path.join(DEPS_DIR, s) for s in LLVM_ARCHIVE_FILES]

KEYSERVER = "pgpkeys.mit.edu"
MIN_PLUMBUM_VERSION = (1, 6, 3)
CMAKELISTS_COMMANDS = \
"""
include_directories({prefix}/include)
link_directories({prefix}/lib)
add_subdirectory(ast-extractor)
""".format(prefix=CBOR_PREFIX)  # nopep8


def on_mac() -> bool:
    """
    return true on macOS/OS X.
    """
    return 'Darwin' in platform.platform()


def on_ubuntu() -> bool:
    """
    return true on recent ubuntu linux distro.
    """
    match = re.match(r'^.+Ubuntu-\d\d\.\d\d-\w+', platform.platform())
    return match is not None


def die(emsg, ecode=1):
    """
    log fatal error and exit with specified error code.
    """
    logging.fatal("error: %s", emsg)
    quit(ecode)


def invoke(cmd, *arguments):
    return _invoke(True, cmd, *arguments)


def invoke_quietly(cmd, *arguments):
    return _invoke(False, cmd, *arguments)


def _invoke(console_output, cmd, *arguments):
    try:
        if console_output:
            retcode, stdout, stderr = cmd[arguments] & pb.TEE()
        else:
            retcode, stdout, stderr = cmd[arguments].run()

        if stdout:
            logging.debug("stdout from %s:\n%s", cmd, stdout)
        if stderr:
            logging.debug("stderr from %s:\n%s", cmd, stderr)

        return retcode, stdout, stderr
    except pb.ProcessExecutionError as pee:
        msg = "cmd exited with code {}: {}".format(pee.retcode, cmd[arguments])
        logging.critical(pee.stderr)
        die(msg, pee.retcode)


def get_cmd_or_die(cmd):
    """
    lookup named command or terminate script.
    """
    try:
        return pb.local[cmd]
    except pb.CommandNotFound:
        die("{} not in path".format(cmd), errno.ENOENT)


def check_sig(afile, asigfile):
    # on macOS, run `brew install gpg`
    gpg = get_cmd_or_die("gpg")

    # make sure we have the right public key installed
    gpg("--keyserver", KEYSERVER, "--recv-key", LLVM_PUBKEY)

    # check that archive matches signature
    try:
        expected = "Good signature from \"Tom Stellard <tom@stellard.net>\""
        logging.debug("checking signature of %s", os.path.basename(afile))
        retcode, _, stderr = gpg['--verify', asigfile, afile].run(retcode=None)
        if retcode:
            die("gpg signature check failed: gpg exit code " + str(retcode))
        if expected not in stderr:
            die("gpg signature check failed: expected signature not found")
    except pb.ProcessExecutionError as pee:
        die("gpg signature check failed: " + pee.message)


def download_llvm_sources():
    curl = get_cmd_or_die("curl")
    tar = get_cmd_or_die("tar")

    with pb.local.cwd(DEPS_DIR):
        # download archives and signatures
        for (aurl, asig, afile, _) in zip(
                LLVM_ARCHIVE_URLS,
                LLVM_SIGNATURE_URLS,
                LLVM_ARCHIVE_FILES,
                LLVM_ARCHIVE_DIRS):

            # download archive
            if not os.path.isfile(afile):
                logging.info("downloading %s", os.path.basename(afile))
                curl(aurl, "-o", afile)
            else:
                continue

            # download archive signature
            asigfile = afile + ".sig"
            if not os.path.isfile(asig):
                logging.debug("downloading %s", asigfile)
                curl(asig, "-o", asigfile)

            check_sig(afile, asigfile)

    # first extract llvm archive
    if not os.path.isdir(LLVM_SRC):
        logging.info("extracting %s", LLVM_ARCHIVE_FILES[0])
        tar("xf", LLVM_ARCHIVE_FILES[0])
        os.rename(LLVM_ARCHIVE_DIRS[0], LLVM_SRC)

    # then clang front end
    with pb.local.cwd(os.path.join(LLVM_SRC, "tools")):
        if not os.path.isdir("clang"):
            logging.info("extracting %s", LLVM_ARCHIVE_FILES[1])
            tar("xf", os.path.join(ROOT_DIR, LLVM_ARCHIVE_FILES[1]))
            os.rename(LLVM_ARCHIVE_DIRS[1], "clang")

        with pb.local.cwd("clang/tools"):
            if not os.path.isdir("extra"):
                logging.info("extracting %s", LLVM_ARCHIVE_FILES[2])
                tar("xf", os.path.join(ROOT_DIR, LLVM_ARCHIVE_FILES[2]))
                os.rename(LLVM_ARCHIVE_DIRS[2], "extra")


def get_ninja_build_type(ninja_build_file):
    signature = "# CMAKE generated file: DO NOT EDIT!" + os.linesep
    with open(ninja_build_file, "r") as handle:
        lines = handle.readlines()
        if not lines[0] == signature:
            die("unexpected content in ninja.build: " + ninja_build_file)
        r = re.compile(r'^#\s*Configuration:\s*(\w+)')
        for line in lines:
            m = r.match(line)
            if m:
                # print m.group(1)
                return m.group(1)
        die("missing content in ninja.build: " + ninja_build_file)


def configure_and_build_llvm(args):
    """
    run cmake as needed to generate ninja buildfiles. then run ninja.
    """
    ninja = get_cmd_or_die("ninja")
    build_type = "Debug" if args.debug else "Release"
    ninja_build_file = os.path.join(LLVM_BLD, "build.ninja")
    with pb.local.cwd(LLVM_BLD):
        if os.path.isfile(ninja_build_file):
            prev_build_type = get_ninja_build_type(ninja_build_file)
            run_cmake = prev_build_type != build_type
        else:
            run_cmake = True

        if run_cmake:
            cmake = get_cmd_or_die("cmake")
            invoke(cmake["-G", "Ninja", LLVM_SRC,
                         "-Wno-dev",
                         "-DCMAKE_C_COMPILER=clang",
                         "-DCMAKE_CXX_COMPILER=clang++",
                         "-DLLVM_BUILD_TESTS=ON",
                         "-DCMAKE_BUILD_TYPE=" + build_type,
                         "-DLLVM_ENABLE_ASSERTIONS=1",
                         "-DLLVM_TARGETS_TO_BUILD=X86"])
        else:
            logging.debug("found existing ninja.build, not running cmake")

        invoke(ninja['ast-extractor'])


def setup_logging():
    logging.basicConfig(
        filename=sys.argv[0].replace(".py", ".log"),
        filemode='w',
        level=logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logging.root.addHandler(console)


def ensure_dir(path):
    if not os.path.exists(path):
        logging.debug("creating dir %s", path)
        os.makedirs(path, mode=0o744)
    if not os.path.isdir(path):
        die("%s is not a directory", path)


def update_cmakelists(filepath):
    if not os.path.isfile(filepath):
        die("not found: " + filepath, errno.ENOENT)
    indicator = "add_subdirectory(ast-extractor)"

    with open(filepath, "r") as handle:
        cmakelists = handle.readlines()
        add_commands = not any([indicator in l for l in cmakelists])
        logging.debug("add commands to %s: %s", filepath, add_commands)

    if add_commands:
        with open(filepath, "a+") as handle:
            handle.writelines(CMAKELISTS_COMMANDS)
        logging.debug("added commands to %s", filepath)


def update_cbor_prefix(makefile):
    """
    rewrite prefix variable in tinycbor makefile.
    """
    if not os.path.isfile(makefile):
        die("not found: " + makefile, errno.ENOENT)

    lines = []
    writeback = False
    with open(makefile, 'r') as fh:
        for line in fh.readlines():
            m = re.match(r'^\s*prefix\s*=\s*([^\s]+)', line)
            if m:
                logging.debug("tinycbor prefix: '%s'", m.group(1))
                prefix = m.group(1)
                writeback = prefix != CBOR_PREFIX
                lines.append("prefix = " + CBOR_PREFIX + os.linesep)
            else:
                lines.append(line)

    if writeback:
        logging.debug("updating tinycbor Makefile")
        with open(makefile, 'w') as fh:
            fh.writelines("".join(lines))


def build_a_bear():
    """
    the output of bear differs between versions, so we build the
    latest bear rather than trying to support multiple versions.
    FIXME: might be better to handle multiple versions instead.
    """
    if os.path.isdir(BEAR_PREFIX):
        logging.debug("skipping Bear installation")
        return

    # download
    if not os.path.isfile(BEAR_ARCHIVE):
        curl = get_cmd_or_die("curl")
        curl['-s', BEAR_URL, '-o', BEAR_ARCHIVE] & pb.TEE

    # unpack
    if not os.path.isdir(BEAR_SRC):
        tar = get_cmd_or_die("tar")
        with pb.local.cwd(DEPS_DIR):
            tar['xf', BEAR_ARCHIVE] & pb.TEE

    # cmake
    bear_build_dir = os.path.join(BEAR_SRC, "build")
    bear_install_prefix = "-DCMAKE_INSTALL_PREFIX=" + BEAR_PREFIX
    ensure_dir(bear_build_dir)
    with pb.local.cwd(bear_build_dir):
        cmake = get_cmd_or_die("cmake")
        cmake["..", bear_install_prefix] & pb.TEE
        make = get_cmd_or_die("make")
        make["install"] & pb.TEE


def install_tinycbor() -> Union[str, None]:
    """
    download, unpack, build, and install tinycbor.
    """
    def path_to_cc_db():
        cc_cmd_db = os.path.join(CBOR_SRC, "compile_commands.json")
        if not os.path.isfile(cc_cmd_db) and not on_mac():
            die("not found: " + cc_cmd_db)
        return cc_cmd_db

    if os.path.isdir(CBOR_PREFIX):
        logging.debug("skipping tinycbor installation")
        return path_to_cc_db()

    # download
    if not os.path.isfile(CBOR_ARCHIVE):
        curl = get_cmd_or_die("curl")
        curl['-s', CBOR_URL, '-o', CBOR_ARCHIVE] & pb.TEE

    # unpack
    if not os.path.isdir(CBOR_SRC):
        tar = get_cmd_or_die("tar")
        with pb.local.cwd(DEPS_DIR):
            tar['xf', CBOR_ARCHIVE] & pb.TEE

    # update install prefix
    update_cbor_prefix(os.path.join(CBOR_SRC, "Makefile"))

    # make && install
    # NOTE: we use bear to wrap make invocations such that
    # we get a .json database of compiler commands that we
    # can use to test ast-extractor. On macOS, bear requires
    # system integrity protection to be turned off, so we
    # only use bear on Ubuntu Linux hosts.
    with pb.local.cwd(CBOR_SRC):
        make = get_cmd_or_die("make")
        if not on_mac():
            bear = get_cmd_or_die(BEAR_BIN)
            make = bear[make]
        make & pb.TEE  # nopep8
        make('install')  # & pb.TEE

    return path_to_cc_db()


def integrate_ast_extractor():
    """
    link ast-extractor into $LLVM_SRC/tools/clang/tools/extra
    """
    src = os.path.join(ROOT_DIR, "ast-extractor")
    extractor_dest = os.path.join(
        LLVM_SRC, "tools/clang/tools/extra/ast-extractor")
    clang_tools_extra = os.path.abspath(
        os.path.join(extractor_dest, os.pardir))
    if not os.path.exists(extractor_dest):
        # NOTE: using os.symlink to emulate `ln -s` would be unwieldy
        ln = get_cmd_or_die("ln")
        with pb.local.cwd(clang_tools_extra):
            ln("-s", src)
    assert os.path.islink(extractor_dest), \
        "missing link: %s->%s" % (src, extractor_dest)

    cmakelists_path = os.path.join(clang_tools_extra, "CMakeLists.txt")
    update_cmakelists(cmakelists_path)


def _parse_args():
    """
    define and parse command line arguments here.
    """
    desc = 'download dependencies for the AST extractor and built it.'
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('-c', '--clean-all', default=False,
                        action='store_true', dest='clean_all',
                        help='clean everything before building')
    dhelp = 'build in debug mode (default build is release+asserts)'
    parser.add_argument('-d', '--debug', default=False,
                        action='store_true', dest='debug',
                        help=dhelp)
    thelp = 'sanity test ast extractor using tinycbor (linux only)'
    parser.add_argument('-t', '--test', default=False,
                        action='store_true', dest='sanity_test',
                        help=thelp)
    return parser.parse_args()


def get_system_include_dirs() -> List[str]:
    """
    note: assumes code was compiled with clang installed locally.
    """
    cc = get_cmd_or_die("clang") 
    cmd = cc["-E", "-Wp,-v", "-"]
    _, _, stderr = cmd.run()
    dirs = stderr.split(os.linesep)
    return [l.strip() for l in dirs if len(l) and l[0] == ' ']


def extract_ast_from(ast_extr: pb.commands.BaseCommand,
                     cc_db_path: str,
                     sys_incl_dirs: List[str],
                     **kwargs) -> None:
    """
    run ast-extractor for a single compiler invocation.

    :param ast_extr: command object representing ast-extractor
    :param cc_db_path: path/to/compile_commands.json
    :param sys_incl_dirs: list of system include directories
    :return: path to generated cbor file.
    """
    # keys = ['arguments', 'directory', 'file']
    keys = ['directory', 'file']  # 'arguments' is not required
    try:
        dir, filename = [kwargs[k] for k in keys]
        filepath = os.path.join(dir, filename)
    except KeyError:
        die("couldn't parse " + cc_db_path)

    if not os.path.isfile(filepath):
        die("missing file " + filepath)
    try:
        # prepare ast-extractor arguments
        cc_db_dir = os.path.dirname(cc_db_path)
        args = ["-p", cc_db_dir, filepath]
        # this is required to locate system libraries
        args += ["-extra-arg=-I" + i for i in sys_incl_dirs]

        # run ast-extractor
        logging.info("extracting ast from %s", os.path.basename(filename))
        ast_extr[args] & pb.TEE  # nopep8
        cbor_outfile = filepath + ".cbor"
        assert os.path.isfile(cbor_outfile), "missing: " + cbor_outfile
        return cbor_outfile
    except pb.ProcessExecutionError as pee:
        if pee.retcode >= 0:
            mesg = os.strerror(pee.retcode)
        else:
            mesg = "Received signal: "
            mesg += signal.Signals(-pee.retcode).name

        logging.fatal("command failed: %s", 
                      ast_extr["-p", cc_db_dir, filename])
        die(u"sanity testing failed 🔥 : " + mesg, pee.retcode)


def test_ast_extractor(cc_db_path: str):
    """
    run ast-extractor on tinycbor if on linux. testing is
    not supported on macOS since bear requires system integrity
    protection to be disabled.
    """
    assert not on_mac(), "sanity testing requires linux host"

    ast_extr = os.path.join(LLVM_BIN, "ast-extractor")
    if not os.path.isfile(ast_extr):
        die("ast-extractor not found in " + LLVM_BIN)
    ast_extr = get_cmd_or_die(ast_extr)

    include_dirs = get_system_include_dirs()

    with open(cc_db_path, "r") as handle:
        cc_db = json.load(handle)
    cbor_files = [extract_ast_from(ast_extr, cc_db_path, include_dirs, **cmd)
                  for cmd in cc_db]

    logging.info(u"sanity test passed 👍")


def binary_in_path(binary_name) -> bool:
    try:
        # raises CommandNotFound exception if not available.
        _ = pb.local[binary_name]
        return True
    except pb.CommandNotFound:
        return False


def _main():
    setup_logging()
    logging.debug("args: %s", " ".join(sys.argv))

    # FIXME: allow env/cli override of LLVM_SRC, LLVM_VER, and LLVM_BLD
    # FIXME: check that cmake and ninja are installed
    # FIXME: option to build LLVM/Clang from master?

    ensure_dir(LLVM_BLD)
    ensure_dir(DEPS_DIR)

    # earlier plumbum versions are missing features such as TEE
    if pb.__version__ < MIN_PLUMBUM_VERSION:
        err = "locally installed version {} of plumbum is too old.\n" \
            .format(pb.__version__)
        err += "please upgrade plumbum to version {} or later." \
            .format(MIN_PLUMBUM_VERSION)
        die(err)

    if on_ubuntu():
        build_a_bear()
        if not os.path.isfile(BEAR_BIN):
            die("bear not found", errno.ENOENT)

    args = _parse_args()
    if args.clean_all:
        logging.info("cleaning all dependencies and previous built files")
        shutil.rmtree(LLVM_SRC, ignore_errors=True)
        shutil.rmtree(LLVM_BLD, ignore_errors=True)
        shutil.rmtree(DEPS_DIR, ignore_errors=True)

    download_llvm_sources()

    integrate_ast_extractor()

    cc_db = install_tinycbor()

    configure_and_build_llvm(args)

    if not on_mac() and args.sanity_test:
        test_ast_extractor(cc_db)

if __name__ == "__main__":
    _main()