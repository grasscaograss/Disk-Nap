import fcntl
import os
from contextlib import contextmanager
from uuid import uuid4

from .macos import SleepError

F_NOCACHE = 48  # Darwin fcntl.h
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def opened(path, flags, *, dir_fd=None):
    descriptor = os.open(path, flags, 0o600, dir_fd=dir_fd)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def open_volume(volume):
    # Once the descriptor is open, a changed mount path cannot redirect
    # relative operations to an empty directory on the system disk.
    with opened(volume.mount_point, DIRECTORY_FLAGS) as descriptor:
        if os.fstat(descriptor).st_dev != os.stat("/dev/" + volume.device).st_rdev:
            raise SleepError("The mount point is not backed by the expected partition.")
        yield descriptor


class IOFixture:
    """A uniquely named scratch directory used for optional write and readback."""

    def __init__(self):
        self.name = f".disk-nap-{uuid4().hex}"
        self.payload = os.urandom(4096)
        self.created = False

    def create(self, volume):
        with open_volume(volume) as root:
            os.mkdir(self.name, mode=0o700, dir_fd=root)
            self.created = True
            with opened(self.name, DIRECTORY_FLAGS, dir_fd=root) as directory:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                with opened("fixture", flags, dir_fd=directory) as descriptor:
                    self.write(descriptor, self.payload)

    @staticmethod
    def write(descriptor, payload):
        with os.fdopen(os.dup(descriptor), "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def read(directory, length):
        with opened("fixture", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory) as descriptor:
            fcntl.fcntl(descriptor, F_NOCACHE, 1)
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                return stream.read(length + 1)

    def verify(self, volume):
        with open_volume(volume) as root:
            with opened(self.name, DIRECTORY_FLAGS, dir_fd=root) as directory:
                if self.read(directory, len(self.payload)) != self.payload:
                    raise SleepError("The pre-suspend fixture did not match after remount.")
                suffix = os.urandom(4096)
                with opened("fixture", os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
                            dir_fd=directory) as descriptor:
                    self.write(descriptor, suffix)
                expected = self.payload + suffix
                if self.read(directory, len(expected)) != expected:
                    raise SleepError("The synced post-resume write did not match on readback.")

    def clean(self, volume):
        with open_volume(volume) as root:
            with opened(self.name, DIRECTORY_FLAGS, dir_fd=root) as directory:
                try:
                    os.unlink("fixture", dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.rmdir(self.name, dir_fd=root)
