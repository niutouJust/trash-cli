import errno
import os
import sys
import random
from datetime import datetime

from .fstab import volumes
from .trash import EX_OK, EX_IOERR, home_trash_dir, volume_trash_dir1, \
    volume_trash_dir2
from .trash import backup_file_path_from
from .trash import logger as trash_logger
from .trash import version


def main():
    return TrashPutCmd(
        sys.stdout,
        sys.stderr,
        os.environ,
        volumes,
        parent_path,
        os.path.realpath,
        RealFs(),
        os.getuid,
        datetime.now
    ).run(sys.argv)

def parent_path(path):
    return os.path.realpath(os.path.dirname(path))

class TrashPutCmd:
    def __init__(self,
                 stdout,
                 stderr,
                 environ,
                 volumes,
                 parent_path,
                 realpath,
                 fs,
                 getuid,
                 now):
        self.stdout      = stdout
        self.stderr      = stderr
        self.environ     = environ
        self.volumes     = volumes
        self.fs          = fs
        self.getuid      = getuid
        self.now         = now
        self.parent_path = parent_path
        self.realpath    = realpath

    def run(self, argv):
        program_name  = os.path.basename(argv[0])

        logger = MyLogger(self.stderr, program_name)

        parser = self.get_option_parser(program_name)
        try:
            (options, args) = parser.parse_args(argv[1:])

            if len(args) <= 0:
                parser.error("Please specify the files to trash.")
        except SystemExit as e:
            return e.code
        else:
            if options.verbose: logger.be_verbose()
            if options.trashdir:
                self.trashdir = options.trashdir

            self.ignore_missing = options.ignore_missing
            self.reporter = TrashPutReporter(logger)
            self.logger = trash_logger
            self.trash_all(args)

            return self.reporter.exit_code()

    def get_option_parser(self, program_name):
        from optparse import OptionParser, SUPPRESS_HELP

        parser = OptionParser(prog=program_name,
                              usage="%prog [OPTION]... FILE...",
                              description="Put files in trash",
                              version="%%prog %s" % version,
                              formatter=NoWrapFormatter(),
                              epilog="""\
To remove a file whose name starts with a '-', for example '-foo',
use one of these commands:

    trash -- -foo

    trash ./-foo

Report bugs to https://github.com/andreafrancia/trash-cli/issues""")

        parser.add_option("-d", "--directory",
                          action="store_true",
                          help="ignored (for GNU rm compatibility)")
        parser.add_option("-f", "--force",
                          action="store_true",
                          dest="ignore_missing",
                          help="silently ignore nonexistent files")
        parser.add_option("-i", "--interactive",
                          action="store_true",
                          help="ignored (for GNU rm compatibility)")
        parser.add_option("-r", "-R", "--recursive",
                          action="store_true",
                          help="ignored (for GNU rm compatibility)")
        parser.add_option("--trash-dir",
                          type='string',
                          action="store", dest='trashdir',
                          help='use TRASHDIR as trash folder')
        parser.add_option("-v",
                          "--verbose",
                          action="store_true",
                          dest="verbose",
                          help="explain what is being done")
        original_print_help = parser.print_help
        def patched_print_help():
            original_print_help(self.stdout)
        def patched_error(msg):
            parser.print_usage(self.stderr)
            parser.exit(2, "%s: error: %s\n" % (program_name, msg))
        def patched_exit(status=0, msg=None):
            if msg: self.stderr.write(msg)
            import sys
            sys.exit(status)

        parser.print_help = patched_print_help
        parser.error = patched_error
        parser.exit = patched_exit
        return parser

    def trash_all(self, args):
        for arg in args :
            self.trash(arg)

    def trash(self, file) :
        """
        Trash a file in the appropriate trash directory.
        If the file belong to the same volume of the trash home directory it
        will be trashed in the home trash directory.
        Otherwise it will be trashed in one of the relevant volume trash
        directories.

        Each volume can have two trash directories, they are
            - $volume/.Trash/$uid
            - $volume/.Trash-$uid

        Firstly the software attempt to trash the file in the first directory
        then try to trash in the second trash directory.
        """

        if self._should_skipped_by_specs(file):
            self.reporter.unable_to_trash_dot_entries(file)
            return

        if self.ignore_missing and not os.access(file, os.F_OK):
            return

        volume_of_file_to_be_trashed = self.volume_of_parent(file)
        self.reporter.volume_of_file(volume_of_file_to_be_trashed)
        candidates = self._possible_trash_directories_for(
                        volume_of_file_to_be_trashed)

        self.try_trash_file_using_candidates(file,
                                             volume_of_file_to_be_trashed,
                                             candidates)

    def try_trash_file_using_candidates(self,
                                        file,
                                        volume_of_file_to_be_trashed,
                                        candidates):
        file_has_been_trashed = False
        for path, volume, path_maker, checker in candidates:
            suffix = Suffix(random.randint)
            info_dir_path = os.path.join(path, 'info')
            info_dir = InfoDir(info_dir_path, self.fs, self.logger, suffix)
            trash_dir = TrashDirectoryForPut(path,
                                             volume,
                                             self.fs,
                                             path_maker(volume),
                                             info_dir)
            if self._is_trash_dir_secure(trash_dir.path, checker):
                volume_of_trash_dir = self.volumes.volume_of(self.realpath(trash_dir.path))
                self.reporter.trash_dir_with_volume(trash_dir.path,
                                                    volume_of_trash_dir)
                if self._file_could_be_trashed_in(volume_of_file_to_be_trashed,
                                                  volume_of_trash_dir):
                    try:
                        trash_dir.trash2(file, self.now)
                        self.reporter.file_has_been_trashed_in_as(
                            file,
                            trash_dir.path)
                        file_has_been_trashed = True

                    except (IOError, OSError) as error:
                        self.reporter.unable_to_trash_file_in_because(
                                file, trash_dir.path, str(error))

            if file_has_been_trashed: break

        if not file_has_been_trashed:
            self.reporter.unable_to_trash_file(file)

    def volume_of_parent(self, file):
        return self.volumes.volume_of(self.parent_path(file))

    def _is_trash_dir_secure(self, trash_dir_path, checker):
        class ValidationOutput:
            def __init__(self, reporter):
                self.valid = True
                self.reporter = reporter
            def not_valid_should_be_a_dir(self):
                self.reporter.invalid_top_trash_is_not_a_dir(
                        os.path.dirname(trash_dir_path))
                self.valid = False
            def not_valid_parent_should_not_be_a_symlink(self):
                self.reporter.found_unsercure_trash_dir_symlink(
                        os.path.dirname(trash_dir_path))
                self.valid = False
            def not_valid_parent_should_be_sticky(self):
                self.reporter.found_unsecure_trash_dir_unsticky(
                        os.path.dirname(trash_dir_path))
                self.valid = False
            def is_valid(self):
                self.valid = True
        output = ValidationOutput(self.reporter)
        checker.check_trash_dir_is_secure(trash_dir_path, output, self.fs)
        return output.valid

    def _should_skipped_by_specs(self, file):
        basename = os.path.basename(file)
        return (basename == ".") or (basename == "..")

    def _file_could_be_trashed_in(self,
                                  volume_of_file_to_be_trashed,
                                  volume_of_trash_dir):
        return volume_of_trash_dir == volume_of_file_to_be_trashed

    def _possible_trash_directories_for(self, volume):
        trash_dirs = []
        def add_home_trash(path, volume):
            path_maker = AbsolutePaths
            checker = all_is_ok_rules
            trash_dirs.append((path, volume, path_maker, checker))
        def add_top_trash_dir(path, volume):
            path_maker = TopDirRelativePaths
            checker = top_trash_dir_rules
            trash_dirs.append((path, volume, path_maker, checker))
        def add_alt_top_trash_dir(path, volume):
            path_maker = TopDirRelativePaths
            checker = all_is_ok_rules
            trash_dirs.append((path, volume, path_maker, checker))

        if hasattr(self, 'trashdir') and self.trashdir:
            path = self.trashdir
            volume = self.volumes.volume_of(path)
            path_maker = TopDirRelativePaths
            checker = all_is_ok_rules
            trash_dirs.append((path, volume, path_maker, checker))
        else:
            for path, dir_volume in home_trash_dir(self.environ,
                                                   self.volumes.volume_of):
                add_home_trash(path, dir_volume)
            for path, dir_volume in volume_trash_dir1(volume, self.getuid):
                add_top_trash_dir(path, dir_volume)
            for path, dir_volume in volume_trash_dir2(volume, self.getuid):
                add_alt_top_trash_dir(path, dir_volume)
        return trash_dirs


def describe(path):
    """
    Return a textual description of the file pointed by this path.
    Options:
     - "symbolic link"
     - "directory"
     - "'.' directory"
     - "'..' directory"
     - "regular file"
     - "regular empty file"
     - "non existent"
     - "entry"
    """
    if os.path.islink(path):
        return 'symbolic link'
    elif os.path.isdir(path):
        if path == '.':
            return 'directory'
        elif path == '..':
            return 'directory'
        else:
            if os.path.basename(path) == '.':
                return "'.' directory"
            elif os.path.basename(path) == '..':
                return "'..' directory"
            else:
                return 'directory'
    elif os.path.isfile(path):
        if os.path.getsize(path) == 0:
            return 'regular empty file'
        else:
            return 'regular file'
    elif not os.path.exists(path):
        return 'non existent'
    else:
        return 'entry'

class MyLogger:
    def __init__(self, stderr, program_name):
        self.program_name = program_name
        self.stderr=stderr
        self.verbose = False
    def be_verbose(self):
        self.verbose = True
    def info(self,message):
        if self.verbose:
            self.emit(message)
    def warning(self,message):
        self.emit(message)
    def emit(self, message):
        self.stderr.write("%s: %s\n" % (self.program_name,message))

from optparse import IndentedHelpFormatter
class NoWrapFormatter(IndentedHelpFormatter) :
    def _format_text(self, text) :
        "[Does not] format a text, return the text as it is."
        return text


class TrashPutReporter:
    def __init__(self, logger):
        self.logger = logger
        self.some_file_has_not_be_trashed = False
        self.no_argument_specified = False
    def unable_to_trash_dot_entries(self,file):
        self.logger.warning("cannot trash %s '%s'" % (describe(file), file))
    def unable_to_trash_file(self,f):
        self.logger.warning("cannot trash %s '%s'" % (describe(f), f))
        self.some_file_has_not_be_trashed = True
    def file_has_been_trashed_in_as(self, trashee, trash_directory):
        self.logger.info("'%s' trashed in %s" % (trashee,
                                                 shrinkuser(trash_directory)))
    def found_unsercure_trash_dir_symlink(self, trash_dir_path):
        self.logger.info("found unsecure .Trash dir (should not be a symlink): %s"
                % trash_dir_path)
    def invalid_top_trash_is_not_a_dir(self, trash_dir_path):
        self.logger.info("found unusable .Trash dir (should be a dir): %s"
                % trash_dir_path)
    def found_unsecure_trash_dir_unsticky(self, trash_dir_path):
        self.logger.info("found unsecure .Trash dir (should be sticky): %s"
                % trash_dir_path)
    def unable_to_trash_file_in_because(self,
                                        file_to_be_trashed,
                                        trash_directory, error):
        self.logger.info("Failed to trash %s in %s, because: %s" % (
           file_to_be_trashed, shrinkuser(trash_directory), error))
    def trash_dir_with_volume(self, trash_dir_path, volume_path):
        self.logger.info("Trash-dir: %s from volume: %s" % (trash_dir_path,
                                                            volume_path))
    def exit_code(self):
        if not self.some_file_has_not_be_trashed:
            return EX_OK
        else:
            return EX_IOERR
    def volume_of_file(self,volume):
        self.logger.info("Volume of file: %s" % volume)

def parent_realpath(path):
    parent = os.path.dirname(path)
    return os.path.realpath(parent)


class TrashDirectoryForPut:
    def __init__(self, path, volume, fs, path_maker, info_dir):
        self.path = os.path.normpath(path)
        self.volume = volume
        self.files_dir = os.path.join(self.path, 'files')
        self.fs = fs
        self.path_maker = path_maker
        self.info_dir = info_dir

    def trash2(self, path, now):
        path = os.path.normpath(path)

        original_location = self.path_for_trash_info_for_file(path)

        basename = os.path.basename(original_location)
        content = format_trashinfo(original_location, now())
        trash_info_file = self.info_dir.persist_trash_info(basename, content)

        where_to_store_trashed_file = backup_file_path_from(trash_info_file)

        self.ensure_files_dir_exists()

        try:
            self.fs.move(path, where_to_store_trashed_file)
        except IOError as e:
            self.fs.remove_file(trash_info_file)
            raise e

    def path_for_trash_info_for_file(self, path):
        path_for_trash_info = OriginalLocation(parent_realpath,
                                               self.path_maker)
        return path_for_trash_info.for_file(path)

    def ensure_files_dir_exists(self):
        self.fs.ensure_dir(self.files_dir, 0o700)


class InfoDir:
    def __init__(self, path, fs, logger, suffix):
        self.path = path
        self.fs = fs
        self.logger = logger
        self.suffix = suffix

    def persist_trash_info(self, basename, content):
        """
        Create a .trashinfo file in the $trash/info directory.
        returns the created TrashInfoFile.
        """

        self.fs.ensure_dir(self.path, 0o700)

        index = 0
        name_too_long = False
        while True:
            suffix = self.suffix.suffix_for_index(index)
            trashinfo_basename = create_trashinfo_basename(basename,
                                                           suffix,
                                                           name_too_long)
            trashinfo_path = os.path.join(self.path, trashinfo_basename)
            try:
                self.fs.atomic_write(trashinfo_path, content)
                self.logger.debug(".trashinfo created as %s." % trashinfo_path)
                return trashinfo_path
            except OSError as e:
                if e.errno == errno.ENAMETOOLONG:
                    name_too_long = True
                self.logger.debug("Attempt for creating %s failed." % trashinfo_path)

            index += 1


def create_trashinfo_basename(basename, suffix, name_too_long):
    after_basename = suffix + ".trashinfo"
    if name_too_long:
        truncated_basename = basename[0:len(basename) - len(after_basename)]
    else:
        truncated_basename = basename
    return truncated_basename + after_basename


class Suffix:
    def __init__(self, randint):
        self.randint = randint

    def suffix_for_index(self, index):
        if index == 0:
            return ""
        elif index < 100:
            return "_%s" % index
        else:
            return "_%s" % self.randint(0, 65535)

def format_trashinfo(original_location, deletion_date):
    def format_date(deletion_date):
        return deletion_date.strftime("%Y-%m-%dT%H:%M:%S")
    def format_original_location(original_location):
        try:
            from urllib import quote
        except ImportError:
            from urllib.parse import quote
        return quote(original_location,'/')
    content = ("[Trash Info]\n" +
               "Path=%s\n" % format_original_location(original_location) +
               "DeletionDate=%s\n" % format_date(deletion_date)).encode('utf-8')
    return content

def shrinkuser(path, environ=os.environ):
    import posixpath
    import re
    try:
        home_dir = environ['HOME']
        home_dir = posixpath.normpath(home_dir)
        if home_dir != '':
            path = re.sub('^' + re.escape(home_dir + os.path.sep),
                            '~' + os.path.sep, path)
    except KeyError:
        pass
    return path


class AllIsOkRules:
    def check_trash_dir_is_secure(self, trash_dir_path, output, fs):
        pass


class TopTrashDirRules:
    def check_trash_dir_is_secure(self, trash_dir_path, output, fs):
        parent = os.path.dirname(trash_dir_path)
        if not fs.isdir(parent):
            output.not_valid_should_be_a_dir()
            return
        if fs.islink(parent):
            output.not_valid_parent_should_not_be_a_symlink()
            return
        if not fs.has_sticky_bit(parent):
            output.not_valid_parent_should_be_sticky()
            return
        output.is_valid()


top_trash_dir_rules = TopTrashDirRules()
all_is_ok_rules = AllIsOkRules()

class OriginalLocation:
    def __init__(self, parent_realpath, path_maker):
        self.parent_realpath = parent_realpath
        self.path_maker = path_maker

    def for_file(self, path):
        self.normalized_path = os.path.normpath(path)

        basename = os.path.basename(self.normalized_path)
        parent   = self.parent_realpath(self.normalized_path)

        parent = self.path_maker.calc_parent_path(parent)

        return os.path.join(parent, basename)

class TopDirRelativePaths:
    def __init__(self, topdir):
        self.topdir = topdir
    def calc_parent_path(self, parent):
        if (parent == self.topdir) or parent.startswith(self.topdir+os.path.sep) :
            parent = parent[len(self.topdir+os.path.sep):]
        return parent

class AbsolutePaths:
    def __init__(self, topdir):
        self.topdir = topdir
    def calc_parent_path(self, parent):
        return parent

class RealFs:
    def __init__(self):
        import os
        from . import fs
        self.move           = fs.move
        self.atomic_write   = fs.atomic_write
        self.remove_file    = fs.remove_file
        self.ensure_dir     = fs.ensure_dir
        self.isdir          = os.path.isdir
        self.islink         = os.path.islink
        self.has_sticky_bit = fs.has_sticky_bit

