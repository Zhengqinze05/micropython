#!/usr/bin/env python3
#
# This file is part of the MicroPython project, http://micropython.org/
#
# The MIT License (MIT)
#
# Copyright (c) 2022 Jim Mussared
# Copyright (c) 2019 Damien P. George
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import print_function
import os
import sys
import glob

__all__ = ["ManifestFileError", "ManifestFile"]

# Allow freeze*() etc.
MODE_FREEZE = 1
# Only allow include/require/module/package.
MODE_COMPILE = 2


# In compile mode, .py -> KIND_COMPILE_AS_MPY
# In freeze mode, .py -> KIND_FREEZE_AS_MPY, .mpy->KIND_FREEZE_MPY
KIND_AUTO = 1
# Freeze-mode only, .py -> KIND_FREEZE_AS_MPY, .mpy->KIND_FREEZE_MPY
KIND_FREEZE_AUTO = 2

# Freeze-mode only, The .py file will be frozen as text.
KIND_FREEZE_AS_STR = 3
# Freeze-mode only, The .py file will be compiled and frozen as bytecode.
KIND_FREEZE_AS_MPY = 4
# Freeze-mode only, The .mpy file will be frozen directly.
KIND_FREEZE_MPY = 5
# Compile mode only, the .py file should be compiled to .mpy.
KIND_COMPILE_AS_MPY = 6

# File on the local filesystem.
FILE_TYPE_LOCAL = 1
# URL to file. (TODO)
FILE_TYPE_HTTP = 2


class ManifestFileError(Exception):
    pass


# Turns a dict of options into a object with attributes used to turn the
# kwargs passed to include() and require into the "options" global in the
# included manifest.
#   options = IncludeOptions(foo="bar", blah="stuff")
#   options.foo # "bar"
#   options.blah # "stuff"
class IncludeOptions:
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._defaults = {}

    def defaults(self, **kwargs):
        self._defaults = kwargs

    def __getattr__(self, name):
        return self._kwargs.get(name, self._defaults.get(name, None))


class ManifestFile:
    def __init__(self, mode, path_vars=None):
        # Either MODE_FREEZE or MODE_COMPILE.
        self._mode = mode
        # Path substition variables.
        self._path_vars = path_vars or {}
        # List of files references by this manifest.
        # Tuple of (file_type, full_path, target_path, timestamp, kind, version, opt)
        self._manifest_files = []
        # Don't allow including the same file twice.
        self._visited = set()

    def _resolve_path(self, path):
        # Convert path to an absolute path, applying variable substitutions.
        for name, value in self._path_vars.items():
            if value is not None:
                path = path.replace("$({})".format(name), value)
        return os.path.abspath(path)

    def _manifest_globals(self, kwargs):
        # This is the "API" available to a manifest file.
        return {
            "metadata": self.metadata,
            "include": self.include,
            "require": self.require,
            "package": self.package,
            "module": self.module,
            "freeze": self.freeze,
            "freeze_as_str": self.freeze_as_str,
            "freeze_as_mpy": self.freeze_as_mpy,
            "freeze_mpy": self.freeze_mpy,
            "options": IncludeOptions(**kwargs),
        }

    def files(self):
        return self._manifest_files

    def execute(self, manifest_file):
        if manifest_file.endswith(".py"):
            # Execute file from filesystem.
            self.include(manifest_file)
        else:
            # Execute manifest code snippet.
            try:
                exec(manifest_file, self._manifest_globals({}))
            except Exception as er:
                raise ManifestFileError("Error in manifest: {}".format(er))

    def _add_file(self, full_path, target_path, kind=KIND_AUTO, version=None, opt=None):
        # Check file exists and get timestamp.
        try:
            stat = os.stat(full_path)
            timestamp = stat.st_mtime
        except OSError:
            raise ManifestFileError("cannot stat {}".format(full_path))

        # Map the AUTO kinds to their actual kind based on mode and extension.
        _, ext = os.path.splitext(full_path)
        if self._mode == MODE_FREEZE:
            if kind in (
                KIND_AUTO,
                KIND_FREEZE_AUTO,
            ):
                if ext.lower() == ".py":
                    kind = KIND_FREEZE_AS_MPY
                elif ext.lower() == ".mpy":
                    kind = KIND_FREEZE_MPY
        else:
            if kind != KIND_AUTO:
                raise ManifestFileError("Not in freeze mode")
            if ext.lower() != ".py":
                raise ManifestFileError("Expected .py file")
            kind = KIND_COMPILE_AS_MPY

        self._manifest_files.append(
            (FILE_TYPE_LOCAL, full_path, target_path, timestamp, kind, version, opt)
        )

    def _search(self, base_path, package_path, files, exts, kind, opt=None, strict=False):
        base_path = self._resolve_path(base_path)

        if files:
            # Use explicit list of files (relative to package_path).
            for file in files:
                if package_path:
                    file = os.path.join(package_path, file)
                self._add_file(
                    os.path.join(base_path, file), file, kind=kind, version=None, opt=opt
                )
        else:
            if base_path:
                prev_cwd = os.getcwd()
                os.chdir(self._resolve_path(base_path))

            # Find all candidate files.
            for dirpath, _, filenames in os.walk(package_path or ".", followlinks=True):
                for file in filenames:
                    file = os.path.relpath(os.path.join(dirpath, file), ".")
                    _, ext = os.path.splitext(file)
                    if ext.lower() in exts:
                        self._add_file(
                            os.path.join(base_path, file),
                            file,
                            kind=kind,
                            version=None,
                            opt=opt,
                        )
                    elif strict:
                        raise ManifestFileError("Unexpected file type")

            if base_path:
                os.chdir(prev_cwd)

    def metadata(self, description=None, version=None):
        # TODO
        pass

    def include(self, manifest_path, **kwargs):
        """
        Include another manifest.

        The manifest argument can be a string (filename) or an iterable of
        strings.

        Relative paths are resolved with respect to the current manifest file.

        If the path is to a directory, then it implicitly includes the
        manifest.py file inside that directory.

        Optional kwargs can be provided which will be available to the
        included script via the `options` variable.

        e.g. include("path.py", extra_features=True)

        in path.py:
            options.defaults(standard_features=True)

            # freeze minimal modules.
            if options.standard_features:
                # freeze standard modules.
            if options.extra_features:
                # freeze extra modules.
        """
        if not isinstance(manifest_path, str):
            for m in manifest_path:
                self.include(m)
        else:
            manifest_path = self._resolve_path(manifest_path)
            # Including a directory grabs the manifest.py inside it.
            if os.path.isdir(manifest_path):
                manifest_path = os.path.join(manifest_path, "manifest.py")
            if manifest_path in self._visited:
                return
            self._visited.add(manifest_path)
            with open(manifest_path) as f:
                # Make paths relative to this manifest file while processing it.
                # Applies to includes and input files.
                prev_cwd = os.getcwd()
                os.chdir(os.path.dirname(manifest_path))
                try:
                    exec(f.read(), self._manifest_globals(kwargs))
                except Exception as er:
                    raise ManifestFileError(
                        "Error in manifest file: {}: {}".format(manifest_path, er)
                    )
                os.chdir(prev_cwd)

    def require(self, name, version=None, **kwargs):
        """
        Require a module by name from micropython-lib.

        This is a shortcut for
        """
        if self._path_vars["MPY_LIB_DIR"]:
            for manifest_path in glob.glob(
                os.path.join(self._path_vars["MPY_LIB_DIR"], "**", name, "manifest.py"),
                recursive=True,
            ):
                self.include(manifest_path, **kwargs)
                return
            raise ValueError("Library not found in local micropython-lib: {}".format(name))
        else:
            # TODO: HTTP request to obtain URLs from manifest.json.
            raise ValueError("micropython-lib not available for require('{}').", name)

    def package(self, package_path, files=None, base_path=".", opt=None):
        """
        Define a package, optionally restricting to a set of files.

        Simple case, a package in the current directory:
            package("foo")
        will include all .py files in foo, and will be stored as foo/bar/baz.py.

        If the package isn't in the current directory, use base_path:
            package("foo", base_path="src")

        To restrict to certain files in the package use files (note: paths should be relative to the package):
            package("foo", files=["bar/baz.py"])
        """
        # Include "base_path/package_path/**/*.py" --> "package_path/**/*.py"
        self._search(base_path, package_path, files, exts=(".py",), kind=KIND_AUTO, opt=opt)

    def module(self, module_path, base_path=".", opt=None):
        """
        Include a single Python file as a module.

        If the file is in the current directory:
            module("foo.py")

        Otherwise use base_path to locate the file:
            module("foo.py", "src/drivers")
        """
        # Include "base_path/module_path" --> "module_path"
        base_path = self._resolve_path(base_path)
        _, ext = os.path.splitext(module_path)
        if ext.lower() != ".py":
            raise ManifestFileError("module must be .py file")
        # TODO: version None
        self._add_file(os.path.join(base_path, module_path), module_path, version=None, opt=opt)

    def _freeze_internal(self, path, script, exts, kind, opt):
        if script is None:
            self._search(path, None, None, exts=exts, kind=kind, opt=opt)
        elif isinstance(script, str) and os.path.isdir(os.path.join(path, script)):
            self._search(path, script, None, exts=exts, kind=kind, opt=opt)
        elif not isinstance(script, str):
            self._search(path, None, script, exts=exts, kind=kind, opt=opt)
        else:
            self._search(path, None, (script,), exts=exts, kind=kind, opt=opt)

    def freeze(self, path, script=None, opt=None):
        """
        Freeze the input, automatically determining its type.  A .py script
        will be compiled to a .mpy first then frozen, and a .mpy file will be
        frozen directly.

        `path` must be a directory, which is the base directory to _search for
        files from.  When importing the resulting frozen modules, the name of
        the module will start after `path`, ie `path` is excluded from the
        module name.

        If `path` is relative, it is resolved to the current manifest.py.
        Use $(MPY_DIR), $(MPY_LIB_DIR), $(PORT_DIR), $(BOARD_DIR) if you need
        to access specific paths.

        If `script` is None all files in `path` will be frozen.

        If `script` is an iterable then freeze() is called on all items of the
        iterable (with the same `path` and `opt` passed through).

        If `script` is a string then it specifies the file or directory to
        freeze, and can include extra directories before the file or last
        directory.  The file or directory will be _searched for in `path`.  If
        `script` is a directory then all files in that directory will be frozen.

        `opt` is the optimisation level to pass to mpy-cross when compiling .py
        to .mpy.
        """
        self._freeze_internal(path, script, exts=(".py", ".mpy"), kind=KIND_FREEZE_AUTO, opt=opt)

    def freeze_as_str(self, path):
        """
        Freeze the given `path` and all .py scripts within it as a string,
        which will be compiled upon import.
        """
        self._search(path, None, None, exts=(".py"), kind=KIND_FREEZE_AS_STR)

    def freeze_as_mpy(self, path, script=None, opt=None):
        """
        Freeze the input (see above) by first compiling the .py scripts to
        .mpy files, then freezing the resulting .mpy files.
        """
        self._freeze_internal(path, script, exts=(".py"), kind=KIND_FREEZE_AS_MPY, opt=opt)

    def freeze_mpy(self, path, script=None, opt=None):
        """
        Freeze the input (see above), which must be .mpy files that are
        frozen directly.
        """
        self._freeze_internal(path, script, exts=(".mpy"), kind=KIND_FREEZE_MPY, opt=opt)


def main():
    import argparse

    cmd_parser = argparse.ArgumentParser(description="List the files referenced by a manifest.")
    cmd_parser.add_argument("--freeze", action="store_true", help="freeze mode")
    cmd_parser.add_argument("--compile", action="store_true", help="compile mode")
    cmd_parser.add_argument(
        "--lib",
        default=os.path.join(os.path.dirname(__file__), "../lib/micropython-lib"),
        help="path to micropython-lib repo",
    )
    cmd_parser.add_argument("--port", default=None, help="path to port dir")
    cmd_parser.add_argument("--board", default=None, help="path to board dir")
    cmd_parser.add_argument(
        "--top",
        default=os.path.join(os.path.dirname(__file__), ".."),
        help="path to micropython repo",
    )
    cmd_parser.add_argument("files", nargs="+", help="input manifest.py")
    args = cmd_parser.parse_args()

    path_vars = {
        "MPY_DIR": os.path.abspath(args.top) if args.top else None,
        "BOARD_DIR": os.path.abspath(args.board) if args.board else None,
        "PORT_DIR": os.path.abspath(args.port) if args.port else None,
        "MPY_LIB_DIR": os.path.abspath(args.lib) if args.lib else None,
    }

    mode = None
    if args.freeze:
        mode = MODE_FREEZE
    elif args.compile:
        mode = MODE_COMPILE
    else:
        print("Error: No mode specified.", file=sys.stderr)
        exit(1)

    m = ManifestFile(mode, path_vars)
    for manifest_file in args.files:
        try:
            m.execute(manifest_file)
        except ManifestFileError as er:
            print(er, file=sys.stderr)
            exit(1)
    for f in m.files():
        print(f)


if __name__ == "__main__":
    main()