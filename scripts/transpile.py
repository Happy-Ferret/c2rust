#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import cbor
import errno
import shutil
import signal
import logging
import argparse
import platform
import multiprocessing

from typing import List, Union
from concurrent.futures import ThreadPoolExecutor

# FIXME: extract common functions and vars to separate file
from build_ast_extractor import *


def json_pp_obj(json_obj) -> str:
    return json.dumps(json_obj,
                      sort_keys=True,
                      indent=2,
                      separators=(',', ': '))


def try_locate_elf_object(cmd: dict) -> Union[str, None]:
    # first look for -o in compiler command
    if "arguments" in cmd:
        command = " ".join(cmd['arguments'])
    elif "command" in cmd:
        command = cmd['command']
    else:
        die("malformed entry in compile_commands.json:\n" + 
            json_pp_obj(cmd))

    if "directory" not in cmd:
        die("malformed entry in compile_commands.json:\n" + 
            json_pp_obj(cmd))
    dir = cmd['directory']

    # FIXME: assumes that outfile has .o suffix
    m = re.search(r"\s-o\s+([^\0]+\.o)\s", command)
    if m:
        outfile = m.group(1)
        outpath = os.path.join(dir, outfile)        
    else:
        # try replacing suffix of C file with .c
        inpath = os.path.join(dir, cmd['file'])
        outpath = inpath.replace(".c", ".o")

    if os.path.isfile(outpath):
        logging.debug("found output filename: %s", outpath)
        return outpath
    else:
        logging.debug("didn't find output filename for command:\n%s",
                      json_pp_obj(cmd))
        return None


def ensure_code_compiled_with_clang(cc_db: List[dict]):
    # filter non C code commands first
    c_cc_db = [c for c in cc_db if c['file'].endswith(".c")]
    if not len(c_cc_db):
        msg = "didn't find any commands compling C files"
        die(msg)

    obj_files = [try_locate_elf_object(c) for c in c_cc_db]
    readelf = get_cmd_or_die("readelf")
    comment_sections = [(f, readelf('-p', '.comment', f))
                        for f in obj_files if f]
    non_clang_files = [(f, c) for (f, c) in comment_sections
                       if "clang" not in c]

    if len(non_clang_files):
        msg = "some ELF objects were not compiled with clang:\n"
        msg += "\n".join([f for (f, c) in comment_sections])
        die(msg)


def transpile_files(args) -> None:
    ast_extr = os.path.join(LLVM_BIN, "ast-extractor")
    ast_extr = get_cmd_or_die(ast_extr)
    ast_impo = os.path.join(
        ROOT_DIR,
        "ast-importer/target/debug/ast-importer")
    ast_impo = get_cmd_or_die(ast_impo)

    cc_db = json.load(args.commands_json)

    if args.filter:  # skip commands not matching file filter
        cc_db = [c for c in cc_db if args.filter in f['file']]

    ensure_code_compiled_with_clang(cc_db)
    include_dirs = get_system_include_dirs()

    def transpile_single(cmd):
        if args.import_only:
            cbor_file = os.path.join(cmd['directory'], cmd['file'] + ".cbor")
        else:
            cbor_file = extract_ast_from(ast_extr, args.commands_json.name,
                                         include_dirs, **cmd)
        assert os.path.isfile(cbor_file), "missing: " + cbor_file

        # import extracted ast
        with pb.local.env(RUST_BACKTRACE='1'):
            logging.info(" importing ast from %s", os.path.basename(cbor_file))
            retcode, stdout, stderr = invoke_quietly(ast_impo, cbor_file)
            # FIXME: error handling

    if args.jobs == 1:
        for cmd in cc_db:
            transpile_single(cmd)
    else:
        # We use the ThreadPoolExecutor (not ProcesssPoolExecutor) because
        # 1. we spend most of the time outside the python interpreter, and
        # 2. it does not require that shared objects can be pickled.
        with ThreadPoolExecutor(args.jobs) as executor:
            for cmd in cc_db:
                executor.submit(transpile_single, cmd)


def parse_args():
    """
    define and parse command line arguments here.
    """
    desc = 'transpile files in compiler_commands.json.'
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('commands_json', type=argparse.FileType('r'))
    parser.add_argument('-i', '--import-only', default=False,
                        action='store_true', dest='import_only',
                        help='skip ast extraction step')
    parser.add_argument('-f', '--filter', default="",
                        help='only process files matching filter')
    parser.add_argument('-j', '--jobs', type=int, dest="jobs",
                        default=multiprocessing.cpu_count(),
                        help='max number of concurrent jobs')
    return parser.parse_args()


def main():
    setup_logging()
    logging.debug("args: %s", " ".join(sys.argv))

    args = parse_args()
    transpile_files(args)

    logging.info(u"success 👍")

if __name__ == "__main__":
    main()