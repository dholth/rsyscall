from __future__ import annotations
from rsyscall._raw import ffi, lib # type: ignore
import types
import traceback
import pathlib

import math
import importlib.resources
ssh_bootstrap_script_contents = importlib.resources.read_text('rsyscall', 'ssh_bootstrap.sh')

from rsyscall.base import Pointer, RsyscallException, RsyscallHangup
from rsyscall.base import to_local_pointer
from rsyscall.base import SyscallInterface

import rsyscall.base as base
import rsyscall.raw_syscalls as raw_syscall
import rsyscall.memory_abstracted_syscalls as memsys
import rsyscall.memory as memory
import rsyscall.handle as handle
import rsyscall.handle
import rsyscall.far as far
import rsyscall.near as near
from rsyscall.struct import T_serializable, T_struct

from rsyscall.sys.socket import AF, SOCK, SOL, SO
from rsyscall.fcntl import AT, O
from rsyscall.sys.socket import T_addr
from rsyscall.linux.futex import FUTEX_WAITERS, FUTEX_TID_MASK
from rsyscall.sys.mount import MS
from rsyscall.sys.un import SockaddrUn, PathTooLongError
from rsyscall.netinet.in_ import SockaddrIn
from rsyscall.sys.epoll import EpollEvent, EpollEventMask, EpollCtlOp
from rsyscall.sys.wait import ChildCode, UncleanExit, ChildEvent, W
from rsyscall.sys.memfd import MFD
from rsyscall.sched import UnshareFlag
from rsyscall.signal import SigprocmaskHow, Sigaction, Sighandler, Signals
from rsyscall.linux.dirent import Dirent

import random
import string
import abc
import prctl
import socket
import abc
import sys
import os
import typing as t
import struct
import array
import trio
import signal
from dataclasses import dataclass, field
import logging
import fcntl
import errno
import enum
import contextlib
import inspect
logger = logging.getLogger(__name__)

async def direct_syscall(number, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0):
    "Make a syscall directly in the current thread."
    args = (ffi.cast('long', arg1), ffi.cast('long', arg2), ffi.cast('long', arg3),
            ffi.cast('long', arg4), ffi.cast('long', arg5), ffi.cast('long', arg6),
            number)
    ret = lib.rsyscall_raw_syscall(*args)
    return ret

def raise_if_error(response: int) -> None:
    if -4095 < response < 0:
        err = -response
        raise OSError(err, os.strerror(err))

def log_syscall(logger, number, arg1, arg2, arg3, arg4, arg5, arg6) -> None:
    if arg6 == 0:
        if arg5 == 0:
            if arg4 == 0:
                if arg3 == 0:
                    if arg2 == 0:
                        if arg1 == 0:
                            logger.debug("%s()", number)
                        else:
                            logger.debug("%s(%s)", number, arg1)
                    else:
                        logger.debug("%s(%s, %s)", number, arg1, arg2)
                else:
                    logger.debug("%s(%s, %s, %s)", number, arg1, arg2, arg3)
            else:
                logger.debug("%s(%s, %s, %s, %s)", number, arg1, arg2, arg3, arg4)
        else:
            logger.debug("%s(%s, %s, %s, %s, %s)", number, arg1, arg2, arg3, arg4, arg5)
    else:
        logger.debug("%s(%s, %s, %s, %s, %s, %s)", number, arg1, arg2, arg3, arg4, arg5, arg6)

class LocalSyscall(base.SyscallInterface):
    activity_fd = None
    identifier_process = near.Process(os.getpid())
    logger = logging.getLogger("rsyscall.LocalSyscall")
    async def close_interface(self) -> None:
        pass

    async def submit_syscall(self, number, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> near.SyscallResponse:
        raise Exception("not supported for local syscaller")

    async def syscall(self, number, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> int:
        log_syscall(self.logger, number, arg1, arg2, arg3, arg4, arg5, arg6)
        try:
            result = await self._syscall(
                number,
                arg1=int(arg1), arg2=int(arg2), arg3=int(arg3),
                arg4=int(arg4), arg5=int(arg5), arg6=int(arg6))
        except Exception as exn:
            self.logger.debug("%s -> %s", number, exn)
            raise
        else:
            self.logger.debug("%s -> %s", number, result)
            return result

    async def _syscall(self, number: int, arg1: int, arg2: int, arg3: int, arg4: int, arg5: int, arg6: int) -> int:
        ret = await direct_syscall(number, arg1, arg2, arg3, arg4, arg5, arg6)
        raise_if_error(ret)
        return ret

class FunctionPointer:
    "A function pointer."
    def __init__(self, pointer: far.Pointer) -> None:
        self.pointer = pointer

class SignalMask:
    def __init__(self, mask: t.Set[signal.Signals]) -> None:
        self.mask = mask

    def inherit(self) -> 'SignalMask':
        return SignalMask(self.mask)

    def _validate(self, task: 'Task') -> SyscallInterface:
        if task.sigmask != self:
            raise Exception
        return task.syscall

    async def block(self, task: 'Task', mask: t.Set[signal.Signals]) -> None:
        syscall = self._validate(task)
        old_mask = await memsys.rt_sigprocmask(syscall, task.transport, task.allocator, SigprocmaskHow.BLOCK, mask)
        if self.mask != old_mask:
            raise Exception("SignalMask tracking got out of sync, thought mask was",
                            self.mask, "but was actually", old_mask)
        self.mask = self.mask.union(mask)

    async def unblock(self, task: 'Task', mask: t.Set[signal.Signals]) -> None:
        syscall = self._validate(task)
        old_mask = await memsys.rt_sigprocmask(syscall, task.transport, task.allocator, SigprocmaskHow.UNBLOCK, mask)
        if self.mask != old_mask:
            raise Exception("SignalMask tracking got out of sync, thought mask was",
                            self.mask, "but was actually", old_mask)
        self.mask = self.mask - mask

    async def setmask(self, task: 'Task', mask: t.Set[signal.Signals]) -> None:
        syscall = self._validate(task)
        old_mask = await memsys.rt_sigprocmask(syscall, task.transport, task.allocator, SigprocmaskHow.SETMASK, mask)
        if self.mask != old_mask:
            raise Exception("SignalMask tracking got out of sync, thought mask was",
                            self.mask, "but was actually", old_mask)
        self.mask = mask

T = t.TypeVar('T')
class File:
    """This is the underlying file object referred to by a file descriptor.

    Often, multiple file descriptors in multiple processes can refer
    to the same file object. For example, the stdin/stdout/stderr file
    descriptors will typically all refer to the same file object
    across several processes started by the same shell.

    This is unfortunate, because there are some useful mutations (in
    particular, setting O_NONBLOCK) which we'd like to perform to
    Files, but which might break other users.

    We store whether the File is shared with others with
    "shared". If it is, we can't mutate it.

    """
    shared: bool
    def __init__(self, shared: bool=False, flags: int=None) -> None:
        self.shared = shared

    async def set_nonblock(self, fd: FileDescriptor[File]) -> None:
        if self.shared:
            raise Exception("file object is shared and can't be mutated")
        if fd.file != self:
            raise Exception("can't set a file to nonblocking through a file descriptor that doesn't point to it")
        await raw_syscall.fcntl(fd.task.syscall, fd.pure, fcntl.F_SETFL, O.NONBLOCK)

    async def lseek(self, fd: 'FileDescriptor[File]', offset: int, whence: int) -> int:
        return (await raw_syscall.lseek(fd.task.syscall, fd.pure, offset, whence))

T_file = t.TypeVar('T_file', bound=File)
T_file_co = t.TypeVar('T_file_co', bound=File, covariant=True)

class TypedPointer(handle.Pointer[T_serializable]):
    def __init__(self, task: Task, data_cls: t.Type[T_serializable], allocation: handle.AllocationInterface) -> None:
        super().__init__(task.base, data_cls, allocation)
        self.mem_task = task

    async def write(self, data: T_serializable) -> None:
        data_bytes = data.to_bytes()
        if len(data_bytes) > self.bytesize():
            raise Exception("data is too long", len(data_bytes),
                            "for this typed pointer of size", self.bytesize())
        await self.mem_task.transport.write(self.far, data_bytes)

    async def read(self) -> T_serializable:
        data = await self.mem_task.transport.read(self.far, self.bytesize())
        return self.data_cls.from_bytes(data)

    def split(self, size: int) -> t.Tuple[TypedPointer[T_serializable], TypedPointer]:
        first_s, second_s = super().split(size)
        first = TypedPointer(self.mem_task, first_s.data_cls, first_s.allocation)
        second = TypedPointer(self.mem_task, second_s.data_cls, second_s.allocation)
        return first, second

class Task:
    def __init__(self,
                 base_: base.Task,
                 transport: base.MemoryTransport,
                 allocator: memory.AllocatorClient,
                 sigmask: SignalMask,
    ) -> None:
        self.base = base_
        self.transport = transport
        # Being able to allocate memory is like having a stack.
        # we really need to be able to allocate memory to get anything done - namely, to call syscalls.
        self.allocator = allocator
        self.sigmask = sigmask

    @property
    def syscall(self) -> base.SyscallInterface:
        return self.base.sysif

    @property
    def address_space(self) -> base.AddressSpace:
        return self.base.address_space

    @property
    def fd_table(self) -> base.FDTable:
        return self.base.fd_table

    def root(self) -> Path:
        return Path(self, handle.Path("/"))

    def cwd(self) -> Path:
        return Path(self, handle.Path("."))

    async def close(self):
        await self.syscall.close_interface()

    async def malloc_struct(self, cls: t.Type[T_struct]) -> TypedPointer[T_struct]:
        return await self.malloc_type(cls, cls.sizeof())

    async def malloc_type(self, cls: t.Type[T_serializable], size: int) -> TypedPointer[T_serializable]:
        allocation = await self.allocator.malloc(size)
        try:
            return TypedPointer(self, cls, allocation)
        except:
            allocation.free()
            raise

    async def to_pointer(self, data: T_serializable) -> TypedPointer[T_serializable]:
        ptr = await self.malloc_type(type(data), len(data.to_bytes()))
        try:
            await ptr.write(data)
        except:
            ptr.free()
            raise
        return ptr

    async def mount(self, source: bytes, target: bytes,
                    filesystemtype: bytes, mountflags: int,
                    data: bytes) -> None:
        serializer = memsys.Serializer()
        source_ptr = serializer.serialize_null_terminated_data(source)
        target_ptr = serializer.serialize_null_terminated_data(target)
        filesystemtype_ptr = serializer.serialize_null_terminated_data(filesystemtype)
        data_ptr = serializer.serialize_null_terminated_data(data)
        async with serializer.with_flushed(self.transport, self.allocator):
            await near.mount(self.base.sysif,
                             source_ptr.pointer.near, target_ptr.pointer.near, filesystemtype_ptr.pointer.near,
                             mountflags, data_ptr.pointer.near)

    async def exit(self, status: int) -> None:
        await raw_syscall.exit(self.syscall, status)
        await self.close()

    async def chdir(self, path: 'Path') -> None:
        with (await self.to_pointer(path.handle)) as ptr:
            await self.base.chdir(ptr)

    async def fchdir(self, fd: handle.FileDescriptor) -> None:
        await self.base.fs.fchdir(self.base, fd.far)

    async def unshare_fs(self) -> None:
        # TODO we want this to return something that we can use to chdir
        await self.base.unshare_fs()

    def _make_fd(self, num: int, file: T_file) -> FileDescriptor[T_file]:
        return self.make_fd(near.FileDescriptor(num), file)

    def make_fd(self, fd: near.FileDescriptor, file: T_file) -> FileDescriptor[T_file]:
        return FileDescriptor(self, self.base.make_fd_handle(fd), file)

    async def open(self, path: handle.Path, flags: int, mode=0o644) -> handle.FileDescriptor:
        """Open a path

        Note that this can block forever if we're opening a FIFO

        """
        with (await self.to_pointer(path)) as ptr:
            return await self.base.open(ptr, flags, mode)

    async def memfd_create(self, name: t.Union[bytes, str]) -> FileDescriptor[MemoryFile]:
        fd = await memsys.memfd_create(
            self.base, self.transport, self.allocator,
            os.fsencode(name), MFD.CLOEXEC)
        return FileDescriptor(self, self.base.make_fd_handle(fd), MemoryFile())

    async def read(self, fd: far.FileDescriptor, count: int=4096) -> bytes:
        return (await memsys.read(self.base, self.transport, self.allocator, fd, count))

    async def pread(self, fd: handle.FileDescriptor, count: int, offset: int) -> bytes:
        return (await memsys.pread(self.base, self.transport, self.allocator, fd.far, count, offset))

    # TODO maybe we'll put these calls as methods on a MemoryAbstractor,
    # and they'll take an handle.FileDescriptor.
    # then we'll directly have StandardTask contain both Task and MemoryAbstractor?
    async def getdents(self, fd: far.FileDescriptor, count: int=4096) -> t.List[Dirent]:
        data = await memsys.getdents64(self.base, self.transport, self.allocator, fd, count)
        return Dirent.list_from_bytes(data)

    async def pipe(self, flags=O.CLOEXEC) -> Pipe:
        r, w = await memsys.pipe(self.syscall, self.transport, self.allocator, flags)
        return Pipe(self._make_fd(r, ReadableFile(shared=False)),
                    self._make_fd(w, WritableFile(shared=False)))

    async def socketpair(self, domain: int, type: int, protocol: int
    ) -> t.Tuple[FileDescriptor[ReadableWritableFile], FileDescriptor[ReadableWritableFile]]:
        l, r = await memsys.socketpair(self.syscall, self.transport, self.allocator,
                                       domain, type|SOCK.CLOEXEC, protocol)
        return (self._make_fd(l, ReadableWritableFile(shared=False)),
                self._make_fd(r, ReadableWritableFile(shared=False)))

    async def epoll_create(self, flags=lib.EPOLL_CLOEXEC) -> FileDescriptor[EpollFile]:
        epfd = await raw_syscall.epoll_create(self.syscall, flags)
        return self._make_fd(epfd, EpollFile())

    async def inotify_init(self, flags=lib.IN_CLOEXEC) -> FileDescriptor[InotifyFile]:
        epfd = await near.inotify_init(self.syscall, flags)
        return self.make_fd(epfd, InotifyFile())

    async def socket_unix(self, type: SOCK, protocol: int=0, cloexec=True) -> FileDescriptor[UnixSocketFile]:
        sockfd = await self.base.socket(AF.UNIX, type, protocol, cloexec=cloexec)
        return FileDescriptor(self, sockfd, UnixSocketFile())

    async def socket_inet(self, type: SOCK, protocol: int=0) -> FileDescriptor[InetSocketFile]:
        sockfd = await self.base.socket(AF.INET, type, protocol)
        return FileDescriptor(self, sockfd, InetSocketFile())

    async def signalfd_create(self, mask: t.Set[signal.Signals], flags: int=0) -> FileDescriptor[SignalFile]:
        sigfd = await memsys.signalfd(self.syscall, self.transport, self.allocator, mask, O.CLOEXEC|flags)
        return self._make_fd(sigfd, SignalFile(mask))

    async def mmap(self, length: int, prot: memory.ProtFlag, flags: memory.MapFlag) -> memory.AnonymousMapping:
        # currently doesn't support specifying an address, nor specifying a file descriptor
        return (await memory.AnonymousMapping.make(self.base, length, prot, flags))

    async def make_epoll_center(self) -> EpollCenter:
        epfd = await self.epoll_create()
        if self.syscall.activity_fd is not None:
            epoll_waiter = EpollWaiter(self, epfd.handle, None)
            epoll_center = EpollCenter(epoll_waiter, epfd.handle, self)
            activity_fd = self.base.make_fd_handle(self.syscall.activity_fd)
            await epoll_waiter.update_activity_fd(activity_fd)
        else:
            # TODO this is a pretty low-level detail, not sure where is the right place to do this
            async def wait_readable():
                logger.debug("wait_readable(%s)", epfd.handle.near.number)
                await trio.hazmat.wait_readable(epfd.handle.near.number)
            epoll_waiter = EpollWaiter(self, epfd.handle, wait_readable)
            epoll_center = EpollCenter(epoll_waiter, epfd.handle, self)
        return epoll_center

    async def getuid(self) -> int:
        return (await near.getuid(self.base.sysif))

    async def getgid(self) -> int:
        return (await near.getgid(self.base.sysif))


class ReadableFile(File):
    async def read(self, fd: 'FileDescriptor[ReadableFile]', count: int=4096) -> bytes:
        return (await fd.task.read(fd.handle.far, count))

class WritableFile(File):
    async def write(self, fd: 'FileDescriptor[WritableFile]', buf: bytes) -> int:
        return (await memsys.write(fd.task.syscall, fd.task.transport, fd.task.allocator, fd.pure, buf))

class SeekableFile(File):
    pass

class ReadableWritableFile(ReadableFile, WritableFile):
    pass

class SignalFile(ReadableFile):
    def __init__(self, mask: t.Set[signal.Signals], shared=False) -> None:
        super().__init__(shared=shared)
        self.mask = mask

    async def signalfd(self, fd: 'FileDescriptor[SignalFile]', mask: t.Set[signal.Signals]) -> None:
        await memsys.signalfd(fd.task.syscall, fd.task.transport, fd.task.allocator, mask, 0, fd=fd.pure)
        self.mask = mask

class MemoryFile(ReadableWritableFile, SeekableFile):
    pass

class DirectoryFile(SeekableFile):
    async def getdents(self, fd: 'FileDescriptor[DirectoryFile]', count: int) -> t.List[Dirent]:
        return (await fd.task.getdents(fd.handle.far, count))

class SocketFile(t.Generic[T_addr], ReadableWritableFile):
    address_type: t.Type[T_addr]

    async def bind(self, fd: 'FileDescriptor[SocketFile[T_addr]]', addr: T_addr) -> None:
        await memsys.bind(fd.task.syscall, fd.task.transport, fd.task.allocator, fd.pure, addr.to_bytes())

    async def listen(self, fd: 'FileDescriptor[SocketFile]', backlog: int) -> None:
        await raw_syscall.listen(fd.task.syscall, fd.pure, backlog)

    async def connect(self, fd: 'FileDescriptor[SocketFile[T_addr]]', addr: T_addr) -> None:
        await memsys.connect(fd.task.syscall, fd.task.transport, fd.task.allocator, fd.pure, addr.to_bytes())

    async def getsockname(self, fd: 'FileDescriptor[SocketFile[T_addr]]') -> T_addr:
        data = await memsys.getsockname(fd.task.syscall, fd.task.transport, fd.task.allocator, fd.pure, self.address_type.addrlen)
        return self.address_type.parse(data)

    async def getpeername(self, fd: 'FileDescriptor[SocketFile[T_addr]]') -> T_addr:
        data = await memsys.getpeername(fd.task.syscall, fd.task.transport, fd.task.allocator, fd.pure, self.address_type.addrlen)
        return self.address_type.parse(data)

    async def getsockopt(self, fd: 'FileDescriptor[SocketFile[T_addr]]', level: int, optname: int, optlen: int) -> bytes:
        return (await memsys.getsockopt(fd.task.syscall, fd.task.transport, fd.task.allocator, fd.pure, level, optname, optlen))

    async def setsockopt(self, fd: 'FileDescriptor[SocketFile[T_addr]]', level: int, optname: int,
                         optval: t.Union[bytes, int]) -> None:
        if isinstance(optval, bytes):
            optbytes = optval
        else:
            optbytes = struct.pack('i', optval)
        return (await memsys.setsockopt(fd.task.syscall, fd.task.transport, fd.task.allocator,
                                        fd.pure, level, optname, optbytes))

    async def accept(self, fd: 'FileDescriptor[SocketFile[T_addr]]', flags: int) -> t.Tuple['FileDescriptor[SocketFile[T_addr]]', T_addr]:
        fdnum, data = await memsys.accept(fd.task.syscall, fd.task.transport, fd.task.allocator,
                                          fd.pure, self.address_type.addrlen, flags)
        addr = self.address_type.from_bytes(data)
        fd = fd.task.make_fd(near.FileDescriptor(fdnum), type(self)())
        return fd, addr

class UnixSocketFile(SocketFile[SockaddrUn]):
    address_type = SockaddrUn

class InetSocketFile(SocketFile[SockaddrIn]):
    address_type = SockaddrIn

class InotifyFile(ReadableFile):
    pass

class FileDescriptor(t.Generic[T_file_co]):
    "A file descriptor, plus a task to access it from, plus the file object underlying the descriptor."
    task: Task
    file: T_file_co
    def __init__(self, task: Task, handle: handle.FileDescriptor, file: T_file_co) -> None:
        self.task = task
        self.handle = handle
        self.file = file
        self.pure = handle.far
        self.open = True

    async def aclose(self):
        if self.open:
            await self.handle.close()
        else:
            pass

    def __str__(self) -> str:
        return f'FD({self.task}, {self.pure}, {self.file})'

    async def __aenter__(self) -> 'FileDescriptor[T_file_co]':
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.aclose()

    async def invalidate(self) -> None:
        await self.handle.invalidate()
        self.open = False

    async def close(self):
        await self.handle.close()
        self.open = False

    def for_task(self, task: base.Task) -> 'FileDescriptor[T_file_co]':
        if self.open:
            return self.__class__(self.task, task.make_fd_handle(self.handle), self.file)
        else:
            raise Exception("file descriptor already closed")

    def move(self, task: base.Task) -> 'FileDescriptor[T_file_co]':
        if self.open:
            return self.__class__(self.task, self.handle.move(task), self.file)
        else:
            raise Exception("file descriptor already closed")

    async def copy_from(self, source: handle.FileDescriptor, flags=0) -> None:
        if self.handle.task.fd_table != source.task.fd_table:
            raise Exception("two fds are not in the same file descriptor tables",
                            self.handle.task.fd_table, source.task.fd_table)
        if self.handle.near == source.near:
            return
        await source.dup3(self.handle, flags)

    async def replace_with(self, source: handle.FileDescriptor, flags=0) -> None:
        await self.copy_from(source)
        await source.invalidate()

    # These are just helper methods which forward to the method on the underlying file object.
    async def set_nonblock(self: 'FileDescriptor[File]') -> None:
        "Set the O_NONBLOCK flag on the underlying file object"
        await self.file.set_nonblock(self)

    async def read(self: 'FileDescriptor[ReadableFile]', count: int=4096) -> bytes:
        return (await self.file.read(self, count))

    async def write(self: 'FileDescriptor[WritableFile]', buf: bytes) -> int:
        return (await self.file.write(self, buf))

    async def write_all(self: 'FileDescriptor[WritableFile]', buf: bytes) -> None:
        while len(buf) > 0:
            ret = await self.write(buf)
            buf = buf[ret:]

    async def add(self: 'FileDescriptor[EpollFile]', fd: 'FileDescriptor', event: Pointer) -> None:
        await self.file.add(self, fd, event)

    async def modify(self: 'FileDescriptor[EpollFile]', fd: 'FileDescriptor', event: Pointer) -> None:
        await self.file.modify(self, fd, event)

    async def delete(self: 'FileDescriptor[EpollFile]', fd: 'FileDescriptor') -> None:
        await self.file.delete(self, fd)

    async def wait(self: 'FileDescriptor[EpollFile]',
                   events: Pointer, maxevents: int, timeout: int) -> int:
        return (await self.file.wait(self, events, maxevents, timeout))

    async def getdents(self: 'FileDescriptor[DirectoryFile]', count: int=4096) -> t.List[Dirent]:
        return (await self.file.getdents(self, count))

    async def lseek(self: 'FileDescriptor[SeekableFile]', offset: int, whence: int) -> int:
        return (await self.file.lseek(self, offset, whence))

    async def signalfd(self: 'FileDescriptor[SignalFile]', mask: t.Set[signal.Signals]) -> None:
        await self.file.signalfd(self, mask)

    async def bind(self: 'FileDescriptor[SocketFile[T_addr]]', addr: T_addr) -> None:
        await self.file.bind(self, addr)

    async def listen(self: 'FileDescriptor[SocketFile]', backlog: int) -> None:
        await self.file.listen(self, backlog)

    async def connect(self: 'FileDescriptor[SocketFile[T_addr]]', addr: T_addr) -> None:
        await self.file.connect(self, addr)

    async def getsockname(self: 'FileDescriptor[SocketFile[T_addr]]') -> T_addr:
        return (await self.file.getsockname(self))

    async def getpeername(self: 'FileDescriptor[SocketFile[T_addr]]') -> T_addr:
        return (await self.file.getpeername(self))

    async def setsockopt(self: 'FileDescriptor[SocketFile[T_addr]]', level: int, optname: int,
                         optval: t.Union[bytes, int]) -> None:
        await self.file.setsockopt(self, level, optname, optval)

    async def getsockopt(self: 'FileDescriptor[SocketFile[T_addr]]', level: int, optname: int, optlen: int) -> bytes:
        return (await self.file.getsockopt(self, level, optname, optlen))

    async def accept(self: FileDescriptor[SocketFile[T_addr]], flags: int) -> t.Tuple[FileDescriptor[SocketFile[T_addr]], T_addr]:
        return (await self.file.accept(self, flags))

class EpollFile(File):
    async def add(self, epfd: FileDescriptor['EpollFile'], fd: FileDescriptor, event: Pointer) -> None:
        await raw_syscall.epoll_ctl(epfd.task.syscall, epfd.pure, EpollCtlOp.ADD, fd.pure, event)

    async def modify(self, epfd: FileDescriptor['EpollFile'], fd: FileDescriptor, event: Pointer) -> None:
        await raw_syscall.epoll_ctl(epfd.task.syscall, epfd.pure, EpollCtlOp.MOD, fd.pure, event)

    async def delete(self, epfd: FileDescriptor['EpollFile'], fd: FileDescriptor) -> None:
        await raw_syscall.epoll_ctl(epfd.task.syscall, epfd.pure, EpollCtlOp.DEL, fd.pure)

    async def wait(self, epfd: 'FileDescriptor[EpollFile]',
                   events: Pointer, maxevents: int, timeout: int) -> int:
        return (await raw_syscall.epoll_wait(epfd.task.syscall, epfd.pure, events, maxevents, timeout))

class EpolledFileDescriptor:
    def __init__(self,
                 epoll_center: EpollCenter,
                 fd: handle.FileDescriptor,
                 queue: trio.abc.ReceiveChannel,
                 number: int) -> None:
        self.epoll_center = epoll_center
        self.fd = fd
        self.queue = queue
        self.number = number
        self.in_epollfd = True

    async def modify(self, events: EpollEventMask) -> None:
        await self.epoll_center.modify(self.fd.far, EpollEvent(self.number, events))

    async def wait(self) -> t.List[EpollEvent]:
        while True:
            try:
                return [self.queue.receive_nowait()]
            except trio.WouldBlock:
                await self.epoll_center.epoller.do_wait()

    async def aclose(self) -> None:
        if self.in_epollfd:
            # TODO hmm, I guess we need to serialize this removal with calls to epoll?
            await self.epoll_center.delete(self.fd.far)
            self.in_epollfd = False
        await self.fd.invalidate()

class EpollCenter:
    "Terribly named class that allows registering fds on epoll, and waiting on them"
    def __init__(self, epoller: EpollWaiter, epfd: handle.FileDescriptor,
                 task: Task) -> None:
        self.epoller = epoller
        self.epfd = epfd
        self.task = task

    def inherit(self, task: Task) -> EpollCenter:
        return EpollCenter(self.epoller,
                           task.base.make_fd_handle(self.epfd),
                           task)

    async def register(self, fd: handle.FileDescriptor, events: EpollEventMask=None) -> EpolledFileDescriptor:
        if events is None:
            events = EpollEventMask.make()
        send, receive = trio.open_memory_channel(math.inf)
        number = self.epoller.add_and_allocate_number(send)
        await self.add(fd.far, EpollEvent(number, events))
        return EpolledFileDescriptor(self, fd, receive, number)

    async def add(self, fd: far.FileDescriptor, event: EpollEvent) -> None:
        await memsys.epoll_ctl_add(self.epfd.task, self.task.transport, self.task.allocator, self.epfd.far, fd, event)

    async def modify(self, fd: far.FileDescriptor, event: EpollEvent) -> None:
        await memsys.epoll_ctl_mod(self.epfd.task, self.task.transport, self.task.allocator, self.epfd.far, fd, event)

    async def delete(self, fd: far.FileDescriptor) -> None:
        await memsys.epoll_ctl_del(self.epfd.task, self.epfd.far, fd)

@dataclass
class PendingEpollWait:
    allocation: memory.Allocation
    syscall_response: near.SyscallResponse
    memory_transport: base.MemoryTransport
    received_events: t.Optional[t.List[EpollEvent]] = None

    async def receive(self) -> t.List[EpollEvent]:
        if self.received_events is not None:
            return self.received_events
        else:
            count = await self.syscall_response.receive()
            bufsize = self.allocation.end - self.allocation.start
            localbuf = await self.memory_transport.read(self.allocation.pointer, bufsize)
            ret: t.List[EpollEvent] = []
            cur = 0
            for _ in range(count):
                ret.append(EpollEvent.from_bytes(localbuf[cur:cur+EpollEvent.bytesize()]))
                cur += EpollEvent.bytesize()
            self.received_events = ret
            self.allocation.free()
            return ret

class EpollWaiter:
    def __init__(self, task: Task, epfd: handle.FileDescriptor,
                 wait_readable: t.Optional[t.Callable[[], t.Awaitable[None]]]) -> None:
        self.waiting_task = task
        self.epfd = epfd
        self.wait_readable = wait_readable
        self.activity_fd: t.Optional[handle.FileDescriptor] = None
        # we reserve 0 for the activity fd
        self.activity_fd_data = 0
        self.next_number = 1
        self.number_to_queue: t.Dict[int, trio.abc.SendChannel] = {}
        self.running_wait = OneAtATime()
        self.pending_epoll_wait: t.Optional[PendingEpollWait] = None

    # need to also support removing, I guess!
    def add_and_allocate_number(self, queue: trio.abc.SendChannel) -> int:
        number = self.next_number
        self.next_number += 1
        self.number_to_queue[number] = queue
        return number

    async def update_activity_fd(self, fd: handle.FileDescriptor) -> None:
        if self.activity_fd is not None:
            # del old activity fd 
            await memsys.epoll_ctl_del(self.epfd.task, self.epfd.far, self.activity_fd.far)
        # add new activity fd
        fd = fd.task.make_fd_handle(fd)
        await memsys.epoll_ctl_add(self.epfd.task, self.waiting_task.transport, self.waiting_task.allocator,
                                   self.epfd.far, fd.far,
                                   EpollEvent(data=self.activity_fd_data, events=EpollEventMask.make(in_=True)))

    async def do_wait(self) -> None:
        async with self.running_wait.needs_run() as needs_run:
            if needs_run:
                if self.wait_readable is not None:
                    logger.info("sleeping before wait")
                    # yield away first
                    await trio.sleep(0)
                    logger.info("performing a wait")
                    received_events = await self.wait(maxevents=32, timeout=0)
                    logger.info("got from wait %s", received_events)
                    if len(received_events) == 0:
                        await self.wait_readable()
                        # We are only guaranteed to receive events from the following line because
                        # we are careful in our usage of epoll.  Some background: Given that we are
                        # using EPOLLET, we can view epoll_wait as providing us a stream of posedges
                        # for readability, writability, etc. We receive negedges in the form of
                        # EAGAINs when we try to read, write, etc.

                        # We could try to optimistically read or write without first receiving a
                        # posedge from epoll. If the read/write succeeds, that serves as a
                        # posedge. Importantly, that posedge might not then be delivered through
                        # epoll.  The posedge is definitely not delivered through epoll if the fd is
                        # read to EAGAIN before the next epoll call, and may also not be delivered
                        # in other scenarios as well.

                        # This can cause deadlocks.  If a thread is blocked in epoll waiting for
                        # some file to become readable, and some other thread goes ahead and reads
                        # off the posedge from the file itself, then the first thread will never get
                        # woken up since epoll will never have an event for readability.

                        # Also, epoll will be indicated as readable to poll when a posedge is ready
                        # for reading; but if we consume that posedge through reading the fd
                        # directly instead, we won't actually get anything when we call epoll_wait.

                        # Therefore, we must make sure to receive all posedges exclusively through
                        # epoll. We can't optimistically read the file descriptor before we have
                        # actually received an initial posedge. (This doesn't hurt performance that
                        # much because we can still optimistically read the fd if we haven't yet
                        # seen an EAGAIN since the last epoll_wait posedge.)

                        # Given that we follow that behavior, we are guaranteed here to get some
                        # event, since wait_readable returning has informed us that there is a
                        # posedge to be read, and all posedges are consumed exclusively through
                        # epoll.
                        received_events = await self.wait(maxevents=32, timeout=0)
                        # We might not receive events even after all that! The reason is as follows:
                        # epoll can generate multiple posedges for a single fd before we consume the
                        # negedge for that fd. One place this can definitely happen is when we get a
                        # partial read from a pipe - we're supposed to know that that's a negedge,
                        # but we don't. It also can happen in an unavoidable way with signalfd:
                        # signalfd seems to negedge as soon as we read the last signal, but we can't
                        # actually tell we read the last signal. er wait. hm. ah! it's a partial
                        # read... since we're doing a large read on the signalfd...
                        # it's weird. okay. whatever.
                        # signalfd sure is weirdly designed.

                        # So, if we're woken up to read from epoll after seeing that it's readable
                        # due to a second posedge for the same fd, and then we consume the negedge
                        # for that fd, and then we actually do the epoll_wait, we won't get the
                        # second posedge - we won't get anything at all.

                        # TODO maybe we can rearrange how we do epoll, now that we know that we
                        # can't guarantee receiving events on each wait. We might be able to improve
                        # performance. Or maybe we can just start specially handling streams.
                else:
                    if self.pending_epoll_wait is None:
                        pending = await self.submit_wait(maxevents=32, timeout=-1)
                        self.pending_epoll_wait = pending
                    else:
                        pending = self.pending_epoll_wait
                    received_events = await pending.receive()
                    self.pending_epoll_wait = None
                for event in received_events:
                    # TODO would be nice to just send these to a "devnull" queue instead...
                    if event.data != self.activity_fd_data:
                        queue = self.number_to_queue[event.data]
                        queue.send_nowait(event.events)

    async def submit_wait(self, maxevents: int, timeout: int) -> PendingEpollWait:
        allocation = await self.waiting_task.allocator.malloc(maxevents * EpollEvent.bytesize())
        try:
            syscall_response = await self.epfd.task.sysif.submit_syscall(
                near.SYS.epoll_wait, self.epfd.near, allocation.pointer, maxevents, timeout)
        except:
            allocation.free()
            raise
        else:
            return PendingEpollWait(allocation, syscall_response, self.waiting_task.transport)

    async def wait(self, maxevents: int, timeout: int) -> t.List[EpollEvent]:
        bufsize = maxevents * EpollEvent.bytesize()
        with await self.waiting_task.allocator.malloc(bufsize) as events_ptr:
            count = await self.epfd.epoll_wait(events_ptr, maxevents, timeout)
            with trio.open_cancel_scope(shield=True):
                localbuf = await self.waiting_task.transport.read(events_ptr, bufsize)
        ret: t.List[EpollEvent] = []
        cur = 0
        for _ in range(count):
            ret.append(EpollEvent.from_bytes(localbuf[cur:cur+EpollEvent.bytesize()]))
            cur += EpollEvent.bytesize()
        return ret

class AsyncFileDescriptor(t.Generic[T_file_co]):
    epolled: EpolledFileDescriptor

    @staticmethod
    async def make(epoller: EpollCenter, fd: FileDescriptor[T_file], is_nonblock=False) -> 'AsyncFileDescriptor[T_file]':
        if not is_nonblock:
            await fd.set_nonblock()
        epolled = await epoller.register(fd.handle, EpollEventMask.make(
            in_=True, out=True, rdhup=True, pri=True, err=True, hup=True, et=True))
        return AsyncFileDescriptor(epolled, fd)

    def __init__(self, epolled: EpolledFileDescriptor, underlying: FileDescriptor[T_file_co]) -> None:
        self.epolled = epolled
        self.underlying = underlying
        self.running_wait = OneAtATime()
        self.is_readable = False
        self.is_writable = False
        self.read_hangup = False
        self.priority = False
        self.error = False
        self.hangup = False

    async def _wait_once(self):
        async with self.running_wait.needs_run() as needs_run:
            if needs_run:
                events = await self.epolled.wait()
                for event in events:
                    if event.in_:   self.is_readable = True
                    if event.out:   self.is_writable = True
                    if event.rdhup: self.read_hangup = True
                    if event.pri:   self.priority = True
                    if event.err:   self.error = True
                    if event.hup:   self.hangup = True

    def could_read(self) -> bool:
        return self.is_readable or self.read_hangup or self.hangup or self.error

    async def read_nonblock(self: 'AsyncFileDescriptor[ReadableFile]', count: int=4096) -> t.Optional[bytes]:
        if not self.could_read():
            return None
        try:
            return (await self.underlying.read(count))
        except OSError as e:
            if e.errno == errno.EAGAIN:
                self.is_readable = False
                return None
            else:
                raise

    async def read(self: 'AsyncFileDescriptor[ReadableFile]', count: int=4096) -> bytes:
        while True:
            while not self.could_read():
                await self._wait_once()
            data = await self.read_nonblock()
            if data is not None:
                return data

    async def wait_for_rdhup(self: 'AsyncFileDescriptor[ReadableFile]') -> None:
        while not (self.read_hangup or self.hangup):
            await self._wait_once()

    async def read_raw(self, sysif: near.SyscallInterface, fd: near.FileDescriptor, pointer: near.Pointer, count: int) -> int:
        while True:
            while not self.could_read():
                await self._wait_once()
            try:
                return (await near.read(sysif, fd, pointer, count))
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    self.is_readable = False
                else:
                    raise

    async def write(self: 'AsyncFileDescriptor[WritableFile]', buf: bytes) -> None:
        while len(buf) > 0:
            while not (self.is_writable or self.error):
                await self._wait_once()
            try:
                written = await self.underlying.write(buf)
                buf = buf[written:]
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    # TODO this is not really quite right if it's possible to concurrently call methods on this object.
                    # we really need to lock while we're making the async call, right? maybe...
                    self.is_writable = False
                else:
                    raise

    async def write_raw(self, sysif: near.SyscallInterface, fd: near.FileDescriptor, pointer: near.Pointer, count: int) -> int:
        while True:
            while not (self.is_writable or self.error):
                await self._wait_once()
            try:
                return (await near.write(sysif, fd, pointer, count))
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    # TODO this is not really quite right if it's possible to concurrently call methods on this object.
                    # we really need to lock while we're making the async call, right? maybe...
                    self.is_writable = False
                else:
                    raise

    async def accept(self: 'AsyncFileDescriptor[SocketFile[T_addr]]', flags: int=SOCK.CLOEXEC
    ) -> t.Tuple[FileDescriptor[SocketFile[T_addr]], T_addr]:
        while True:
            while not (self.is_readable or self.hangup):
                await self._wait_once()
            try:
                return (await self.underlying.accept(flags))
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    self.is_readable = False
                else:
                    raise

    async def accept_as_async(self: 'AsyncFileDescriptor[SocketFile[T_addr]]'
    ) -> t.Tuple[AsyncFileDescriptor[SocketFile[T_addr]], T_addr]:
        connfd: FileDescriptor[SocketFile[T_addr]]
        addr: T_addr
        connfd, addr = await self.accept(flags=SOCK.CLOEXEC|SOCK.NONBLOCK)
        try:
            aconnfd = await AsyncFileDescriptor.make(
                self.epolled.epoll_center, connfd, is_nonblock=True)
            return aconnfd, addr
        except Exception:
            await connfd.aclose()
            raise

    async def connect(self: 'AsyncFileDescriptor[SocketFile[T_addr]]', addr: T_addr) -> None:
        try:
            await self.underlying.connect(addr)
        except OSError as e:
            if e.errno == errno.EINPROGRESS:
                while not self.is_writable:
                    await self._wait_once()
                retbuf = await self.underlying.getsockopt(SOL.SOCKET, SO.ERROR, ffi.sizeof('int'))
                err = ffi.cast('int*', ffi.from_buffer(retbuf))[0]
                if err != 0:
                    raise OSError(err, os.strerror(err))
            else:
                raise

    async def aclose(self) -> None:
        await self.epolled.aclose()

class Path(rsyscall.path.PathLike):
    "This is a convenient combination of a Path and a Task to perform serialization."
    def __init__(self, task: Task, handle: rsyscall.path.Path) -> None:
        self.task = task
        self.handle = handle
        # we cache the pointer to the serialized path
        self._ptr: t.Optional[rsyscall.handle.Pointer[rsyscall.path.Path]] = None

    def with_task(self, task: Task) -> Path:
        return Path(task, self.handle)

    @property
    def parent(self) -> Path:
        return Path(self.task, self.handle.parent)

    @property
    def name(self) -> str:
        return self.handle.name

    async def to_pointer(self) -> handle.Pointer[rsyscall.path.Path]:
        if self._ptr is None:
            self._ptr = await self.task.to_pointer(self.handle)
        return self._ptr

    async def mkdir(self, mode=0o777) -> Path:
        try:
            await self.task.base.mkdir(await self.to_pointer(), mode)
        except FileExistsError as e:
            raise FileExistsError(e.errno, e.strerror, self) from None
        return self

    async def open(self, flags: int, mode=0o644) -> FileDescriptor:
        """Open a path

        Note that this can block forever if we're opening a FIFO

        """
        file: File
        if flags & O.PATH:
            file = File()
        elif flags & O.WRONLY:
            file = WritableFile()
        elif flags & O.RDWR:
            file = ReadableWritableFile()
        elif flags & O.DIRECTORY:
            file = DirectoryFile()
        else:
            # O.RDONLY is 0, so if we don't have any of the rest, then...
            file = ReadableFile()
        fd = await self.task.open(self.handle, flags, mode)
        return FileDescriptor(self.task, fd, file)

    async def open_directory(self) -> FileDescriptor[DirectoryFile]:
        return (await self.open(O.DIRECTORY))

    async def open_path(self) -> FileDescriptor[File]:
        return (await self.open(O.PATH))

    async def creat(self, mode=0o644) -> FileDescriptor[WritableFile]:
        return await self.open(O.WRONLY|O.CREAT|O.TRUNC, mode)

    async def access(self, *, read=False, write=False, execute=False) -> bool:
        mode = 0
        if read:
            mode |= os.R_OK
        if write:
            mode |= os.W_OK
        if execute:
            mode |= os.X_OK
        # default to os.F_OK
        if mode == 0:
            mode = os.F_OK
        ptr = await self.to_pointer()
        try:
            await self.task.base.access(ptr, mode)
            return True
        except OSError:
            return False

    async def unlink(self, flags: int=0) -> None:
        await self.task.base.unlink(await self.to_pointer())

    async def rmdir(self) -> None:
        await self.task.base.rmdir(await self.to_pointer())

    async def link(self, oldpath: Path, flags: int=0) -> Path:
        "Create a hardlink at Path 'self' to the file at Path 'oldpath'"
        await self.task.base.link(await oldpath.to_pointer(), await self.to_pointer())
        return self

    async def symlink(self, target: t.Union[bytes, str, Path]) -> Path:
        "Create a symlink at Path 'self' pointing to the passed-in target"
        if isinstance(target, Path):
            target_ptr = await target.to_pointer()
        else:
            # TODO should write the bytes directly, rather than going through Path;
            # Path will canonicalize the bytes as a path, which isn't right
            target_ptr = await self.task.to_pointer(handle.Path(os.fsdecode(target)))
        await self.task.base.symlink(target_ptr, await self.to_pointer())
        return self

    async def rename(self, oldpath: Path, flags: int=0) -> Path:
        "Create a file at Path 'self' by renaming the file at Path 'oldpath'"
        await self.task.base.rename(await oldpath.to_pointer(), await self.to_pointer())
        return self

    async def readlink(self) -> Path:
        selfptr = await self.to_pointer()
        size = 4096
        bufptr = await self.task.malloc_type(rsyscall.path.Path, size)
        ret = await self.task.base.readlink(selfptr, bufptr)
        if ret == size:
            # ext4 limits symlinks to this size, so let's just throw if it's larger;
            # we can add retry logic later if we ever need it
            raise Exception("symlink longer than 4096 bytes, giving up on readlinking it")
        pathptr, rest = bufptr.split(ret)
        rest.free()
        # readlink doesn't append a null byte, so unfortunately we can't save this buffer and use it for later calls
        target = await pathptr.read()
        pathptr.free()
        return Path(self.task, target)

    async def canonicalize(self) -> Path:
        async with (await self.open_path()) as f:
            return (await Path(self.task, f.handle.as_proc_path()).readlink())

    # to_bytes and from_bytes, kinda sketchy, hmm....
    # from_bytes will fail at runtime... whatever

    T = t.TypeVar('T', bound='Path')
    def __truediv__(self: T, key: t.Union[str, bytes, pathlib.PurePath]) -> T:
        if isinstance(key, bytes):
            key = os.fsdecode(key)
        return type(self)(self.task, self.handle/key)

    def __fspath__(self) -> str:
        return self.handle.__fspath__()

def random_string(k=8) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=k))

async def update_symlink(parent: Path, name: str, target: str) -> None:
    tmpname = name + ".updating." + random_string()
    tmppath = (parent/tmpname)
    await tmppath.symlink(target)
    await (parent/name).rename(tmppath)

async def robust_unix_bind(path: Path, sock: FileDescriptor[UnixSocketFile]) -> None:
    """Perform a Unix socket bind, hacking around the 108 byte limit on socket addresses.

    If the passed path is too long to fit in an address, this function will open the path's
    directory with O_PATH, and bind to /proc/self/fd/n/{pathname}; if that's still too long due to
    pathname being too long, this function will call robust_unix_bind_helper to bind to a temporary
    name and rename the resulting socket to pathname.

    If you are going to be binding to this path repeatedly, it's more efficient to open the
    directory with O_PATH and call robust_unix_bind_helper yourself, rather than call into this
    function.

    """
    try:
        addr = SockaddrUn.from_path(path)
    except PathTooLongError:
        # shrink the path by opening its parent directly as a dirfd
        async with (await path.parent.open_directory()) as dirfd:
            await bindat(sock, dirfd.handle, path.name)
    else:
        await sock.bind(addr)

async def bindat(sock: FileDescriptor[UnixSocketFile], dirfd: handle.FileDescriptor, name: str) -> None:
    """Perform a Unix socket bind to dirfd/name

    TODO: This hack is actually semantically different from a normal direct bind: it's not
    atomic. That's tricky...

    """
    dir = Path(sock.task, handle.Path("/proc/self/fd")/str(int(dirfd.near)))
    path = dir/name
    try:
        addr = SockaddrUn.from_path(path)
    except PathTooLongError:
        # TODO retry if this name is used
        tmpname = ".temp_for_bindat." + random_string(k=16)
        tmppath = dir/tmpname
        await sock.bind(SockaddrUn.from_path(tmppath))
        await path.rename(tmppath)
    else:
        await sock.bind(addr)

async def robust_unix_connect(path: Path, sock: FileDescriptor[UnixSocketFile]) -> None:
    """Perform a Unix socket connect, hacking around the 108 byte limit on socket addresses.

    If the passed path is too long to fit in an address, this function will open that path with
    O_PATH and connect to /proc/self/fd/n.

    If you are going to be connecting to this path repeatedly, it's more efficient to open the path
    with O_PATH yourself rather than call into this function.

    """
    try:
        addr = SockaddrUn.from_path(path)
    except PathTooLongError:
        async with (await path.open_path()) as fd:
            await connectat(sock, fd.handle)
    else:
        await sock.connect(addr)

async def connectat(sock: FileDescriptor[UnixSocketFile], fd: handle.FileDescriptor) -> None:
    "connect() a Unix socket to the passed-in fd"
    path = handle.Path("/proc/self/fd")/str(int(fd.near))
    addr = SockaddrUn.from_path(path)
    await sock.connect(addr)

@dataclass
class StandardStreams:
    stdin: FileDescriptor[ReadableFile]
    stdout: FileDescriptor[WritableFile]
    stderr: FileDescriptor[WritableFile]

@dataclass
class UnixBootstrap:
    """The resources traditionally given to a process on startup in Unix.

    These are not absolutely guaranteed; environ and stdstreams are
    both userspace conventions. Still, we will rely on this for our
    tasks.

    """
    task: Task
    argv: t.List[bytes]
    environ: t.Mapping[bytes, bytes]
    stdstreams: StandardStreams

def wrap_stdin_out_err(task: Task) -> StandardStreams:
    stdin = task._make_fd(0, ReadableFile(shared=True))
    stdout = task._make_fd(1, WritableFile(shared=True))
    stderr = task._make_fd(2, WritableFile(shared=True))
    return StandardStreams(stdin, stdout, stderr)

@dataclass
class UnixUtilities:
    rm: handle.Path
    sh: handle.Path
    ssh: SSHCommand

async def spit(path: Path, text: t.Union[str, bytes], mode=0o644) -> Path:
    """Open a file, creating and truncating it, and write the passed text to it

    Probably shouldn't use this on FIFOs or anything.

    Returns the passed-in Path so this serves as a nice pseudo-constructor.

    """
    data = os.fsencode(text)
    async with (await path.creat(mode=mode)) as fd:
        while len(data) > 0:
            ret = await fd.write(data)
            data = data[ret:]
    return path

@dataclass
class ProcessResources:
    server_func: FunctionPointer
    persistent_server_func: FunctionPointer
    do_cloexec_func: FunctionPointer
    stop_then_close_func: FunctionPointer
    trampoline_func: FunctionPointer
    futex_helper_func: FunctionPointer

    @staticmethod
    def make_from_symbols(address_space: far.AddressSpace, symbols: t.Any) -> ProcessResources:
        def to_pointer(cffi_ptr) -> FunctionPointer:
            return FunctionPointer(
                far.Pointer(address_space, near.Pointer(int(ffi.cast('ssize_t', cffi_ptr)))))
        return ProcessResources(
            server_func=to_pointer(symbols.rsyscall_server),
            persistent_server_func=to_pointer(symbols.rsyscall_persistent_server),
            do_cloexec_func=to_pointer(symbols.rsyscall_do_cloexec),
            stop_then_close_func=to_pointer(symbols.rsyscall_stop_then_close),
            trampoline_func=to_pointer(symbols.rsyscall_trampoline),
            futex_helper_func=to_pointer(symbols.rsyscall_futex_helper),
        )

    def build_trampoline_stack(self, function: FunctionPointer, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> bytes:
        # TODO clean this up with dicts or tuples or something
        stack_struct = ffi.new('struct rsyscall_trampoline_stack*')
        stack_struct.rdi = int(arg1)
        stack_struct.rsi = int(arg2)
        stack_struct.rdx = int(arg3)
        stack_struct.rcx = int(arg4)
        stack_struct.r8  = int(arg5)
        stack_struct.r9  = int(arg6)
        stack_struct.function = ffi.cast('void*', int(function.pointer.near))
        logger.info("trampoline_func %s", self.trampoline_func.pointer)
        packed_trampoline_addr = struct.pack('Q', int(self.trampoline_func.pointer.near))
        stack = packed_trampoline_addr + bytes(ffi.buffer(stack_struct))
        return stack

trampoline_stack_size = ffi.sizeof('struct rsyscall_trampoline_stack') + 8

local_process_resources = ProcessResources(
    server_func=FunctionPointer(base.cffi_to_local_pointer(lib.rsyscall_server)),
    persistent_server_func=FunctionPointer(base.cffi_to_local_pointer(lib.rsyscall_persistent_server)),
    do_cloexec_func=FunctionPointer(base.cffi_to_local_pointer(lib.rsyscall_do_cloexec)),
    stop_then_close_func=FunctionPointer(base.cffi_to_local_pointer(lib.rsyscall_stop_then_close)),
    trampoline_func=FunctionPointer(base.cffi_to_local_pointer(lib.rsyscall_trampoline)),
    futex_helper_func=FunctionPointer(base.cffi_to_local_pointer(lib.rsyscall_futex_helper)),
)

@dataclass
class FilesystemResources:
    tmpdir: handle.Path
    utilities: UnixUtilities
    # locale?
    # home directory?
    rsyscall_server_path: handle.Path
    socket_binder_path: handle.Path
    rsyscall_bootstrap_path: handle.Path
    rsyscall_stdin_bootstrap_path: handle.Path
    rsyscall_unix_stub_path: handle.Path

    @staticmethod
    def make_from_environ(task: handle.Task, environ: t.Mapping[bytes, bytes]) -> FilesystemResources:
        tmpdir = task.make_path_from_bytes(environ.get(b"TMPDIR", b"/tmp"))
        def cffi_to_path(cffi_char_array) -> handle.Path:
            return task.make_path_from_bytes(ffi.string(cffi_char_array))
        utilities = UnixUtilities(
            rm=cffi_to_path(lib.rm_path),
            sh=cffi_to_path(lib.sh_path),
            ssh=SSHCommand.make(cffi_to_path(lib.ssh_path)),
        )
        rsyscall_pkglibexecdir = cffi_to_path(lib.pkglibexecdir)
        rsyscall_server_path = rsyscall_pkglibexecdir/"rsyscall-server"
        socket_binder_path = rsyscall_pkglibexecdir/"socket-binder"
        rsyscall_bootstrap_path = rsyscall_pkglibexecdir/"rsyscall-bootstrap"
        rsyscall_stdin_bootstrap_path = rsyscall_pkglibexecdir/"rsyscall-stdin-bootstrap"
        return FilesystemResources(
            tmpdir=tmpdir,
            utilities=utilities,
            rsyscall_server_path=rsyscall_server_path,
            socket_binder_path=socket_binder_path,
            rsyscall_bootstrap_path=rsyscall_bootstrap_path,
            rsyscall_stdin_bootstrap_path=rsyscall_stdin_bootstrap_path,
            rsyscall_unix_stub_path=rsyscall_pkglibexecdir/"rsyscall-unix-stub",
        )

async def lookup_executable(paths: t.List[Path], name: bytes) -> Path:
    "Find an executable by this name in this list of paths"
    if b"/" in name:
        raise Exception("name should be a single path element without any / present")
    for path in paths:
        filename = path/name
        if (await filename.access(read=True, execute=True)):
            return filename
    raise Exception("executable not found", name)

async def which(stdtask: StandardTask, name: t.Union[str, bytes]) -> Command:
    "Find an executable by this name in PATH"
    namebytes = os.fsencode(name)
    executable_dirs: t.List[Path] = []
    for prefix in stdtask.environment[b"PATH"].split(b":"):
        executable_dirs.append(Path(stdtask.task, handle.Path(os.fsdecode(prefix))))
    executable_path = await lookup_executable(executable_dirs, namebytes)
    return Command(executable_path.handle, [namebytes], {})

async def write_user_mappings(task: Task, uid: int, gid: int,
                              in_namespace_uid: int=None, in_namespace_gid: int=None) -> None:
    if in_namespace_uid is None:
        in_namespace_uid = uid
    if in_namespace_gid is None:
        in_namespace_gid = gid
    root = task.root()

    uid_map = await (root/"proc"/"self"/"uid_map").open(O.WRONLY)
    await uid_map.write(f"{in_namespace_uid} {uid} 1\n".encode())
    await uid_map.invalidate()

    setgroups = await (root/"proc"/"self"/"setgroups").open(O.WRONLY)
    await setgroups.write(b"deny")
    await setgroups.invalidate()

    gid_map = await (root/"proc"/"self"/"gid_map").open(O.WRONLY)
    await gid_map.write(f"{in_namespace_gid} {gid} 1\n".encode())
    await gid_map.invalidate()

class StandardTask:
    def __init__(self,
                 access_task: Task,
                 access_epoller: EpollCenter,
                 access_connection: t.Optional[t.Tuple[Path, FileDescriptor[UnixSocketFile]]],
                 connecting_task: Task,
                 # TODO we need to lock this, and the access_connection also.
                 # they are shared between processes...
                 connecting_connection: t.Tuple[handle.FileDescriptor, handle.FileDescriptor],
                 task: Task,
                 process_resources: ProcessResources,
                 filesystem_resources: FilesystemResources,
                 epoller: EpollCenter,
                 child_monitor: ChildProcessMonitor,
                 environment: t.Dict[bytes, bytes],
                 stdin: FileDescriptor[ReadableFile],
                 stdout: FileDescriptor[WritableFile],
                 stderr: FileDescriptor[WritableFile],
    ) -> None:
        self.access_task = access_task
        self.access_epoller = access_epoller
        self.access_connection = access_connection
        self.connecting_task = connecting_task
        self.connecting_connection = connecting_connection
        self.task = task
        self.process = process_resources
        self.filesystem = filesystem_resources
        self.epoller = epoller
        self.child_monitor = child_monitor
        self.environment = environment
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr

    @staticmethod
    async def make_local() -> StandardTask:
        syscall = LocalSyscall()
        pid = os.getpid()
        pid_namespace = far.PidNamespace(pid)
        # how do I make a socketpair without a socketpair...
        # guess I can just use the near syscall.
        # oh no I also need to register on the epoller, I can't do that.
        process = far.Process(pid_namespace, near.Process(pid))
        base_task = handle.Task(syscall, process, base.FDTable(pid), base.local_address_space,
                                far.FSInformation(pid, root=near.DirectoryFile(),
                                                  cwd=near.DirectoryFile()),
                                pid_namespace,
                                far.NetNamespace(pid),
        )
        task = Task(base_task,
                    LocalMemoryTransport(),
                    memory.AllocatorClient.make_allocator(base_task),
                    SignalMask(set()))
        environ = {key.encode(): value.encode() for key, value in os.environ.items()}
        stdstreams = wrap_stdin_out_err(task)

        # TODO fix this to... pull it from the bootstrap or something...
        process_resources = local_process_resources
        filesystem_resources = FilesystemResources.make_from_environ(base_task, environ)
        epoller = await task.make_epoll_center()
        child_monitor = await ChildProcessMonitor.make(task, epoller)
        # connection_listening_socket = await task.socket_unix(SOCK.STREAM)
        # sockpath = Path.from_bytes(task, b"./rsyscall.sock")
        # await robust_unix_bind(sockpath, connection_listening_socket)
        # await connection_listening_socket.listen(10)
        # access_connection = (sockpath, connection_listening_socket)
        access_connection = None
        left_fd, right_fd = await task.socketpair(socket.AF_UNIX, SOCK.STREAM, 0)
        connecting_connection = (left_fd.handle, right_fd.handle)
        stdtask = StandardTask(
            task, epoller, access_connection,
            task, connecting_connection,
            task, process_resources, filesystem_resources,
            epoller, child_monitor,
            {**environ},
            stdstreams.stdin,
            stdstreams.stdout,
            stdstreams.stderr,
        )
        # We don't need this ourselves, but we keep it around so others can inherit it.
        [(access_sock, remote_sock)] = await stdtask.make_async_connections(1)
        task.transport = SocketMemoryTransport(access_sock, remote_sock)
        return stdtask

    async def mkdtemp(self, prefix: str="mkdtemp") -> 'TemporaryDirectory':
        parent = Path(self.task, self.filesystem.tmpdir)
        random_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        name = (prefix+"."+random_suffix).encode()
        await (parent/name).mkdir(mode=0o700)
        return TemporaryDirectory(self, parent, name)

    async def spawn_exec(self) -> RsyscallThread:
        rsyscall_thread = await self.fork()
        await rsyscall_exec(self, rsyscall_thread, self.filesystem.rsyscall_server_path)
        return rsyscall_thread

    async def make_async_connections(self, count: int) -> t.List[
            t.Tuple[AsyncFileDescriptor[ReadableWritableFile], handle.FileDescriptor]
    ]:
        conns = await self.make_connections(count)
        access_socks, local_socks = zip(*conns)
        async_access_socks = [await AsyncFileDescriptor.make(self.access_epoller, sock) for sock in access_socks]
        return list(zip(async_access_socks, local_socks))

    async def make_connections(self, count: int) -> t.List[
            t.Tuple[FileDescriptor[ReadableWritableFile], handle.FileDescriptor]
    ]:
        return (await make_connections(
            self.access_task, self.access_connection,
            self.connecting_task, self.connecting_connection,
            self.task, count))

    async def fork(self, newuser=False, newpid=False, fs=True, sighand=True) -> RsyscallThread:
        [(access_sock, remote_sock)] = await self.make_async_connections(1)
        thread_maker = ThreadMaker(self.task, self.child_monitor, self.process)
        task, thread = await spawn_rsyscall_thread(
            access_sock, remote_sock,
            self.task, thread_maker, self.process.server_func,
            newuser=newuser, newpid=newpid, fs=fs, sighand=sighand,
        )
        await remote_sock.invalidate()
        if newuser:
            # hack, we should really track the [ug]id ahead of this so we don't have to get it
            # we have to get the [ug]id from the parent because it will fail in the child
            uid = await near.getuid(self.task.base.sysif)
            gid = await near.getgid(self.task.base.sysif)
            await write_user_mappings(task, uid, gid)
        if newpid or self.child_monitor.is_reaper:
            # if the new process is pid 1, then CLONE_PARENT isn't allowed so we can't use inherit_to_child.
            # if we are a reaper, than we don't want our child CLONE_PARENTing to us, so we can't use inherit_to_child.
            # in both cases we just fall back to making a new ChildProcessMonitor for the child.
            epoller = await task.make_epoll_center()
            # this signal is already blocked, we inherited the block, um... I guess...
            # TODO handle this more formally
            signal_block = SignalBlock(task, {signal.SIGCHLD})
            child_monitor = await ChildProcessMonitor.make(task, epoller, signal_block=signal_block, is_reaper=newpid)
        else:
            epoller = self.epoller.inherit(task)
            child_monitor = self.child_monitor.inherit_to_child(thread.child_task, task.base)
        stdtask = StandardTask(
            self.access_task, self.access_epoller, self.access_connection,
            self.connecting_task,
            (self.connecting_connection[0], task.base.make_fd_handle(self.connecting_connection[1])),
            task, 
            self.process, self.filesystem,
            epoller, child_monitor,
            {**self.environment},
            stdin=self.stdin.for_task(task.base),
            stdout=self.stdout.for_task(task.base),
            stderr=self.stderr.for_task(task.base),
        )
        return RsyscallThread(stdtask, thread)

    async def run(self, command: Command, check=True, *, task_status=trio.TASK_STATUS_IGNORED) -> ChildEvent:
        thread = await self.fork(fs=False)
        child = await command.exec(thread)
        task_status.started(child)
        exit_event = await child.wait_for_exit()
        if check:
            exit_event.check()
        return exit_event

    async def unshare_files(self, going_to_exec=False) -> None:
        """Unshare the file descriptor table.

        Set going_to_exec to True if you are about to exec with this task; then we'll skip the
        manual CLOEXEC in userspace that we have to do to avoid keeping stray references around.

        TODO maybe this should return an object that lets us unset CLOEXEC on things?
        """
        async def do_unshare(close_in_old_space: t.List[near.FileDescriptor],
                             copy_to_new_space: t.List[near.FileDescriptor]) -> None:
            await unshare_files(self.task, self.child_monitor, self.process,
                                close_in_old_space, copy_to_new_space, going_to_exec)
        await self.task.base.unshare_files(do_unshare)

    async def unshare_user(self,
                           in_namespace_uid: int=None, in_namespace_gid: int=None) -> None:
        uid = await self.task.getuid()
        gid = await self.task.getgid()
        await self.task.base.unshare_user()
        await write_user_mappings(self.task, uid, gid,
                                  in_namespace_uid=in_namespace_uid, in_namespace_gid=in_namespace_gid)

    async def unshare_net(self) -> None:
        await self.task.base.unshare_net()

    async def setns_user(self, fd: handle.FileDescriptor) -> None:
        await self.task.base.setns_user(fd)

    async def unshare_mount(self) -> None:
        await rsyscall.near.unshare(self.task.base.sysif, UnshareFlag.NEWNS)

    async def setns_mount(self, fd: handle.FileDescriptor) -> None:
        fd.check_is_for(self.task.base)
        await fd.setns(UnshareFlag.NEWNS)

    async def exit(self, status) -> None:
        await self.task.exit(0)

    async def close(self) -> None:
        await self.task.close()

    async def __aenter__(self) -> 'StandardTask':
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.close()

class TemporaryDirectory:
    path: Path
    def __init__(self, stdtask: StandardTask, parent: Path, name: bytes) -> None:
        self.stdtask = stdtask
        self.parent = parent
        self.name = name
        self.path = parent/name

    async def cleanup(self) -> None:
        # TODO would be nice if not sharing the fs information gave us a cap to chdir
        cleanup_thread = await self.stdtask.fork(fs=False)
        async with cleanup_thread:
            await cleanup_thread.stdtask.task.chdir(self.parent)
            name = os.fsdecode(self.name)
            child = await cleanup_thread.execve(self.stdtask.filesystem.utilities.sh, [
                "sh", "-c", f"chmod -R +w -- {name} && rm -rf -- {name}"])
            await child.check()

    async def __aenter__(self) -> 'Path':
        return self.path

    async def __aexit__(self, *args, **kwargs):
        await self.cleanup()

class SignalBlock:
    """This represents some signals being blocked from normal handling

    We need this around to use alternative signal handling mechanisms
    such as signalfd.

    """
    task: Task
    mask: t.Set[signal.Signals]
    @staticmethod
    async def make(task: Task, mask: t.Set[signal.Signals]) -> 'SignalBlock':
        if len(mask.intersection(task.sigmask.mask)) != 0:
            raise Exception("can't allocate a SignalBlock for a signal that was already blocked",
                            mask, task.sigmask.mask)
        await task.sigmask.block(task, mask)
        return SignalBlock(task, mask)

    def __init__(self, task: Task, mask: t.Set[signal.Signals]) -> None:
        self.task = task
        self.mask = mask

    async def close(self) -> None:
        await self.task.sigmask.unblock(self.task, self.mask)

    async def __aenter__(self) -> 'SignalBlock':
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.close()

class SignalQueue:
    def __init__(self, signal_block: SignalBlock, sigfd: AsyncFileDescriptor[SignalFile]) -> None:
        self.signal_block = signal_block
        self.sigfd = sigfd

    @classmethod
    async def make(cls, task: Task, epoller: EpollCenter, mask: t.Set[signal.Signals],
                   *, signal_block: SignalBlock=None,
    ) -> SignalQueue:
        if signal_block is None:
            signal_block = await SignalBlock.make(task, mask)
        else:
            if signal_block.mask != mask:
                raise Exception("passed-in SignalBlock", signal_block, "has mask", signal_block.mask,
                                "which does not match the mask for the SignalQueue we're making", mask)
        sigfd = await task.signalfd_create(mask)
        async_sigfd = await AsyncFileDescriptor.make(epoller, sigfd)
        return cls(signal_block, async_sigfd)

    async def read(self) -> t.Any:
        data = await self.sigfd.read()
        # lol, have to do this because it seems that ffi.cast drops the reference to the backing buffer.
        dest = ffi.new('struct signalfd_siginfo*')
        src = ffi.cast('struct signalfd_siginfo*', ffi.from_buffer(data))
        ffi.memmove(dest, src, ffi.sizeof('struct signalfd_siginfo'))
        return dest

    async def close(self) -> None:
        await self.signal_block.close()
        await self.sigfd.aclose()

    async def __aenter__(self) -> 'SignalQueue':
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.close()

class MultiplexerQueue:
    "This will be some kinda abstracted queue thing that can be used for epoll and for ChildProcessMonitor etc"
    # TODO
    # maybe we should, uhh
    # oh, we can't just check if someone is running and if they are, starting waiting on the queue
    # because, we need to get woken up to do the run if we're waiting
    # maybe that should be the thing, hmm
    # run this waiting function as long as someone is waiting on the queue
    # run in their time slice
    pass

class Multiplexer:
    pass

class ChildProcess:
    def __init__(self, process: base.Process, child_events_channel: trio.abc.SendChannel,
                 monitor: ChildProcessMonitorInternal) -> None:
        self.process = process
        self.child_events_channel = child_events_channel
        self.monitor = monitor
        self.death_event: t.Optional[ChildEvent] = None

    async def wait(self) -> t.List[ChildEvent]:
        if self.death_event:
            raise Exception("child is already dead!")
        while True:
            try:
                event = self.child_events_channel.receive_nowait()
                if event.died():
                    self.death_event = event
                return [event]
            except trio.WouldBlock:
                await self.monitor.do_wait()

    def _flush_nowait(self) -> None:
        while True:
            try:
                event = self.child_events_channel.receive_nowait()
                if event.died():
                    self.death_event = event
            except trio.WouldBlock:
                return

    async def wait_for_exit(self) -> ChildEvent:
        if self.death_event:
            return self.death_event
        while True:
            for event in (await self.wait()):
                if event.died():
                    return event

    async def check(self) -> ChildEvent:
        death = await self.wait_for_exit()
        death.check()
        return death

    async def wait_for_stop_or_exit(self) -> ChildEvent:
        while True:
            for event in (await self.wait()):
                if event.died():
                    return event
                elif event.code == ChildCode.STOPPED:
                    return event

    @property
    def syscall(self) -> SyscallInterface:
        return self.monitor.signal_queue.sigfd.underlying.task.syscall

    @contextlib.asynccontextmanager
    async def get_pid(self) -> t.AsyncGenerator[t.Optional[base.Process], None]:
        """Returns the underlying process, or None if it's already dead.

        Operating on the pid of a child process requires taking the wait_lock to make sure
        the process's zombie is not collected while we're using its pid.

        """
        # TODO this could really be a reader-writer lock, with this use as the reader and
        # wait as the writer.
        async with self.monitor.wait_lock:
            self._flush_nowait()
            if self.death_event:
                yield None
            else:
                yield self.process

    async def send_signal(self, sig: signal.Signals) -> None:
        async with self.get_pid() as process:
            if process:
                await raw_syscall.kill(self.syscall, process, sig)
            else:
                raise Exception("child is already dead!")

    async def kill(self) -> None:
        async with self.get_pid() as process:
            if process:
                await raw_syscall.kill(self.syscall, process, signal.SIGKILL)

    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, *args, **kwargs) -> None:
        if self.death_event:
            pass
        else:
            await self.kill()
            await self.wait_for_exit()

class ChildProcessMonitorInternal:
    def __init__(self, waiting_task: Task, signal_queue: SignalQueue, is_reaper: bool) -> None:
        self.waiting_task = waiting_task
        self.signal_queue = signal_queue
        self.is_reaper = is_reaper
        self.task_map: t.Dict[int, trio.abc.SendChannel[ChildEvent]] = {}
        self.wait_lock = trio.Lock()
        if self.signal_queue.sigfd.underlying.file.mask != set([signal.SIGCHLD]):
            raise Exception("ChildProcessMonitor should get a SignalQueue only for SIGCHLD")
        self.running_wait = OneAtATime()
        self.can_waitid = False

        self.clone_lock = trio.Lock()
        self.cloning_task: t.Optional[base.Task] = None
        self.waited_on_while_cloning: t.Optional[ChildProcess] = None

    def add_task(self, process: base.Process) -> ChildProcess:
        send, receive = trio.open_memory_channel(math.inf)
        child_task = ChildProcess(process, receive, self)
        self.task_map[process.near.id] = send
        return child_task

    async def clone(self,
                    clone_task: base.Task,
                    flags: int,
                    child_stack: Pointer, ctid: Pointer=None, newtls: Pointer=None) -> ChildProcess:
        # Careful synchronization between calls to clone and calls to wait is required.
        # We can only call clone in one task at a time.
        # See my bloggo posto for more.
        # TODO write my bloggo posto
        if self.is_reaper:
            # if we're a reaper, we can't simultaneously wait and clone.
            lock = self.wait_lock
        else:
            lock = self.clone_lock
        async with lock:
            self.cloning_task = clone_task
            try:
                tid = await raw_syscall.clone(clone_task.sysif, flags|signal.SIGCHLD, child_stack,
                                              ptid=None, ctid=ctid, newtls=newtls)
                waited_on_while_cloning = self.waited_on_while_cloning
            finally:
                self.waited_on_while_cloning = None
                self.cloning_task = None
            if waited_on_while_cloning is not None:
                return waited_on_while_cloning
            else:
                return self.add_task(base.Process(clone_task.pidns, near.Process(tid)))

    async def do_wait(self) -> None:
        async with self.running_wait.needs_run() as needs_run:
            if needs_run:
                if not self.can_waitid:
                    # we don't care what information we get from the signal, we just want to
                    # sleep until a SIGCHLD happens
                    logger.info("doing signal queue read")
                    await self.signal_queue.read()
                    logger.info("done with signal queue read")
                    self.can_waitid = True
                # loop on waitid to flush all child events
                task = self.waiting_task
                # TODO if we could just detect when the ChildProcess that we are wait()ing for
                # has gotten an event, we could handle events in this function indefinitely,
                # and only return once we've sent an event to that ChildProcess.
                # maybe by passing in the waiting queue?
                # could do the same for epoll too.
                # though we have to wake other people up too...
                try:
                    # have to serialize against things which use pids; we can't do a wait
                    # while something else is making a syscall with a pid, because we
                    # might collect the zombie for that pid and cause pid reuse
                    logger.info("taking waitid lock")
                    async with self.wait_lock:
                        logger.info("entering waitid")
                        siginfo = await memsys.waitid(
                            task.syscall, task.transport, task.allocator,
                            None, W.ALL|W.EXITED|W.STOPPED|W.CONTINUED|W.NOHANG)
                        logger.info("done with waitid")
                except ChildProcessError:
                    # no more children
                    logger.info("no more children")
                    self.can_waitid = False
                    return
                struct = ffi.cast('siginfo_t*', ffi.from_buffer(siginfo))
                if struct.si_pid == 0:
                    # no more waitable events, but we still have children
                    logger.info("no more waitable events")
                    self.can_waitid = False
                    return
                child_event = ChildEvent.make(ChildCode(struct.si_code),
                                              pid=int(struct.si_pid), uid=int(struct.si_uid),
                                              status=int(struct.si_status))
                logger.info("got child event %s", child_event)
                pid = child_event.pid
                if pid not in self.task_map:
                    if self.cloning_task is not None:
                        # this is the child we were just cloning. it died before clone returned.
                        child_task = self.add_task(base.Process(self.cloning_task.pidns, near.Process(pid)))
                        self.waited_on_while_cloning = child_task
                        # only one clone happens at a time, if we get more unknown tids, they're a bug
                        self.cloning_task = None
                    else:
                        if not self.is_reaper:
                            raise Exception("got event for some unknown pid", child_event,
                                            "but we weren't configured as a reaper")
                        else:
                            # just ignore events for children that were reparented to us
                            logger.info("got orphaned child event!")
                            print("got orphaned child event!")
                self.task_map[pid].send_nowait(child_event)
                if child_event.died():
                    # this child is dead. if its pid is reused, we don't want to send
                    # any more events to the same ChildProcess.
                    del self.task_map[pid]

    async def close(self) -> None:
        await self.signal_queue.close()

@dataclass
class ChildProcessMonitor:
    internal: ChildProcessMonitorInternal
    cloning_task: base.Task
    use_clone_parent: bool
    is_reaper: bool

    @staticmethod
    async def make(task: Task, epoller: EpollCenter,
                   *, signal_block: SignalBlock=None,
                   is_reaper: bool=False,
    ) -> ChildProcessMonitor:
        signal_queue = await SignalQueue.make(task, epoller, {signal.SIGCHLD}, signal_block=signal_block)
        monitor = ChildProcessMonitorInternal(task, signal_queue, is_reaper=is_reaper)
        return ChildProcessMonitor(monitor, task.base, use_clone_parent=False, is_reaper=is_reaper)

    def inherit_to_child(self, child: ChildProcess, cloning_task: base.Task) -> ChildProcessMonitor:
        if self.is_reaper:
            # TODO we should actually look at something on the Task, I suppose, to determine if we're a reaper
            raise Exception("we're a ChildProcessMonitor for a reaper task, "
                            "we can't be inherited because we can't use CLONE_PARENT")
        if child.monitor is not self.internal:
            raise Exception("child", child, "is not from our monitor", self.internal)
        if child.process is not cloning_task.process:
            raise Exception("child process", child, "is not the same as cloning task process", cloning_task)
        # we now know that the cloning task is in a process which is a child process of the waiting task.  so
        # we know that if use CLONE_PARENT while cloning in the cloning task, the resulting tasks will be
        # children of the waiting task, so we can use the waiting task to wait on them.
        return ChildProcessMonitor(self.internal, cloning_task, use_clone_parent=True, is_reaper=self.is_reaper)

    def inherit_to_thread(self, cloning_task: base.Task) -> ChildProcessMonitor:
        if self.internal.waiting_task.base.process is not cloning_task.process:
            raise Exception("waiting task process", self.internal.waiting_task.base.process,
                            "is not the same as cloning task process", cloning_task.process)
        # we know that the cloning task is in the same process as the waiting task. so any children the
        # cloning task starts will also be waitable-on by the waiting task.
        return ChildProcessMonitor(self.internal, cloning_task, use_clone_parent=False, is_reaper=self.is_reaper)

    async def clone(self, flags: int, child_stack: Pointer, ctid: Pointer=None, newtls: Pointer=None) -> ChildProcess:
        if self.use_clone_parent:
            flags |= lib.CLONE_PARENT
        return (await self.internal.clone(self.cloning_task, flags, child_stack, ctid, newtls))

class Thread:
    """A thread is a child task currently running in the address space of its parent.

    This means:
    1. We have probably allocated memory for it, including a stack and thread-local storage.
    2. We need to free that memory when the task stops existing (by calling exit or receiving a signal)
    3. We need to free that memory when the task calls exec (and leaves our address space)

    We can straightforwardly achieve 2 by monitoring SIGCHLD/waitid for the task.

    To achieve 3, we need some reliable way to know when the task has successfully called
    exec. Since a thread can exec an arbitrary executable, we can't rely on the task notifying us
    when it has finished execing.

    We effectively want to be notified on mm_release. To achieve this, we use CLONE_CHILD_CLEARTID,
    which causes the task to do a futex wakeup on a specified address when it calls mm_release, and
    dedicate another task to waiting on that futex address.

    The purpose of this class, then, is to hold the resources necessary to be notified of
    mm_release. Namely, the futex.

    It would better if we could just get notified of mm_release through SIGCHLD/waitid.

    """
    child_task: ChildProcess
    futex_task: ChildProcess
    futex_mapping: handle.MemoryMapping
    def __init__(self, child_task: ChildProcess, futex_task: ChildProcess, futex_mapping: handle.MemoryMapping) -> None:
        self.child_task = child_task
        self.futex_task = futex_task
        self.futex_mapping = futex_mapping
        self.released = False

    async def execveat(self, sysif: SyscallInterface, transport: base.MemoryTransport, allocator: memory.AllocatorInterface,
                       path: handle.Path, argv: t.List[bytes], envp: t.List[bytes], flags: int) -> ChildProcess:
        await memsys.execveat(sysif, transport, allocator, path, argv, envp, flags)
        return self.child_task

    async def wait_for_mm_release(self) -> ChildProcess:
        """Wait for the task to leave the parent's address space, and return the ChildProcess.

        The task can leave the parent's address space either by exiting or execing.

        """
        # once the futex task has exited, the child task has left the parent's address space.
        result = await self.futex_task.wait_for_exit()
        if not result.clean():
            raise Exception("the futex task", self.futex_task, "for child task", self.child_task,
                            "unexpectedly exited non-zero", result, "maybe it was SIGKILL'd?")
        await self.futex_mapping.munmap()
        self.released = True
        return self.child_task

    async def close(self) -> None:
        if not self.released:
            await self.child_task.kill()
            await self.wait_for_mm_release()

    async def __aenter__(self) -> 'Thread':
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.close()

class CThread(Thread):
    """A thread running the C runtime and some C function.

    At the moment, that means it has a stack. 
    The considerations for the Thread class all therefore apply.

    TODO thread-local-storage.

    """
    stack_mapping: memory.AnonymousMapping
    def __init__(self, thread: Thread, stack_mapping: memory.AnonymousMapping) -> None:
        super().__init__(thread.child_task, thread.futex_task, thread.futex_mapping)
        self.stack_mapping = stack_mapping

    async def wait_for_mm_release(self) -> ChildProcess:
        result = await super().wait_for_mm_release()
        # we can free the stack mapping now that the thread has left our address space
        await self.stack_mapping.unmap()
        return result

    async def __aenter__(self) -> 'CThread':
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.close()

class BufferedStack:
    def __init__(self, base: Pointer) -> None:
        self.base = base
        self.allocation_pointer = self.base
        self.buffer = b""

    def push(self, data: bytes) -> Pointer:
        self.allocation_pointer -= len(data)
        self.buffer = data + self.buffer
        return self.allocation_pointer

    def align(self, alignment=16) -> None:
        offset = int(self.allocation_pointer.near) % alignment
        self.push(bytes(offset))

    async def flush(self, transport: base.MemoryWriter) -> Pointer:
        await transport.write(self.allocation_pointer, self.buffer)
        self.buffer = b""
        return self.allocation_pointer

async def launch_futex_monitor(task: base.Task, transport: base.MemoryTransport, allocator: memory.AllocatorInterface,
                               process_resources: ProcessResources, monitor: ChildProcessMonitor,
                               futex_pointer: Pointer, futex_value: int) -> ChildProcess:
    serializer = memsys.Serializer()
    # build the trampoline and push it on the stack
    stack_data = process_resources.build_trampoline_stack(process_resources.futex_helper_func, futex_pointer, futex_value)
    # TODO we need appropriate alignment here, we're just lucky because the alignment works fine by accident right now
    stack_pointer = serializer.serialize_data(stack_data)
    logger.info("about to serialize")
    async with serializer.with_flushed(transport, allocator):
        logger.info("did serialize")
        futex_task = await monitor.clone(lib.CLONE_VM|lib.CLONE_FILES|signal.SIGCHLD, stack_pointer.pointer)
        # wait for futex helper to SIGSTOP itself,
        # which indicates the trampoline is done and we can deallocate the stack.
        event = await futex_task.wait_for_stop_or_exit()
        if event.died():
            raise Exception("thread internal futex-waiting task died unexpectedly", event)
        # resume the futex_task so it can start waiting on the futex
        await futex_task.send_signal(signal.SIGCONT)
    # the stack will be freed as it is no longer needed, but the futex pointer will live on
    return futex_task

class ThreadMaker:
    def __init__(self,
                 task: Task,
                 monitor: ChildProcessMonitor,
                 process_resources: ProcessResources) -> None:
        self.task = task
        self.monitor = monitor
        # TODO pull this function out of somewhere sensible
        self.process_resources = process_resources

    async def clone(self, flags: int, child_stack: Pointer, newtls: Pointer) -> Thread:
        """Provides an asynchronous interface to the CLONE_CHILD_CLEARTID functionality

        Executes the instruction "ret" immediately after cloning.

        """
        # the mapping is SHARED rather than PRIVATE so that the futex is shared even if CLONE_VM
        # unshares the address space
        # TODO not sure that actually works
        mapping = await far.mmap(self.task.base, 4096, memory.ProtFlag.READ|memory.ProtFlag.WRITE,
                                 memory.MapFlag.SHARED|memory.MapFlag.ANONYMOUS)
        futex_pointer = mapping.as_pointer()
        futex_task = await launch_futex_monitor(
            self.task.base, self.task.transport, self.task.allocator, self.process_resources, self.monitor,
            futex_pointer, 0)
        # the only part of the memory mapping that's being used now is the futex address, which is a
        # huge waste. oh well, later on we can allocate futex addresses out of a shared mapping.
        child_task = await self.monitor.clone(
            flags | lib.CLONE_CHILD_CLEARTID, child_stack,
            ctid=futex_pointer, newtls=newtls)
        return Thread(child_task, futex_task, handle.MemoryMapping(self.task.base, mapping))

    async def make_cthread(self, flags: int,
                          function: FunctionPointer, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0,
    ) -> CThread:
        # allocate memory for the stack
        stack_size = 4096
        mapping = await self.task.mmap(stack_size, memory.ProtFlag.READ|memory.ProtFlag.WRITE, memory.MapFlag.PRIVATE)
        stack = BufferedStack(mapping.pointer + stack_size)
        # build stack
        stack.push(self.process_resources.build_trampoline_stack(function, arg1, arg2, arg3, arg4, arg5, arg6))
        # copy the stack over
        stack_pointer = await stack.flush(self.task.transport)
        # TODO actually allocate TLS
        tls = self.task.address_space.null()
        thread = await self.clone(flags|signal.SIGCHLD, stack_pointer, tls)
        return CThread(thread, mapping)

class RsyscallConnection:
    "A connection to some rsyscall server where we can make syscalls"
    def __init__(self,
                 tofd: AsyncFileDescriptor[WritableFile],
                 fromfd: AsyncFileDescriptor[ReadableFile],
    ) -> None:
        self.tofd = tofd
        self.fromfd = fromfd
        self.buffer = ReadBuffer()

    async def close(self) -> None:
        await self.tofd.aclose()
        await self.fromfd.aclose()

    async def write_request(self, number: int,
                            arg1: int, arg2: int, arg3: int, arg4: int, arg5: int, arg6: int) -> None:
        request = ffi.new('struct rsyscall_syscall*',
                          (number, (arg1, arg2, arg3, arg4, arg5, arg6)))
        try:
            await self.tofd.write(bytes(ffi.buffer(request)))
        except OSError as e:
            # we raise a different exception so that users can distinguish syscall errors from
            # transport errors
            raise RsyscallException() from e

    async def read_response(self) -> int:
        size = ffi.sizeof('unsigned long')
        response_bytes = self.buffer.read_length(size)
        while response_bytes is None:
            new_data = await self.fromfd.read()
            if len(new_data) == 0:
                raise RsyscallHangup()
            self.buffer.feed_bytes(new_data)
            response_bytes = self.buffer.read_length(size)
        else:
            response, = struct.unpack('q', response_bytes)
            return response

class ChildExit(RsyscallHangup):
    pass

class MMRelease(RsyscallHangup):
    pass

@dataclass
class SyscallResponse(near.SyscallResponse):
    process_one_response: t.Any
    result: t.Optional[t.Union[Exception, int]] = None

    async def receive(self) -> int:
        while self.result is None:
            await self.process_one_response()
        else:
            if isinstance(self.result, int):
                return self.result
            else:
                raise self.result

    def set_exception(self, exn: Exception) -> None:
        if self.result is not None:
            raise Exception("trying to set result on SyscallResponse twice")
        self.result = exn

    def set_result(self, result: int) -> None:
        if self.result is not None:
            raise Exception("trying to set result on SyscallResponse twice")
        self.result = result

class ReadBuffer:
    def __init__(self) -> None:
        self.buf = b""

    def feed_bytes(self, data: bytes) -> None:
        self.buf += data

    def read_length(self, length: int) -> t.Optional[bytes]:
        if length <= len(self.buf):
            section = self.buf[:length]
            self.buf = self.buf[length:]
            return section
        else:
            return None

class ChildConnection(base.SyscallInterface):
    "A connection to some rsyscall server where we can make syscalls"
    def __init__(self,
                 rsyscall_connection: RsyscallConnection,
                 server_task: ChildProcess,
                 futex_task: t.Optional[ChildProcess],
    ) -> None:
        self.rsyscall_connection = rsyscall_connection
        self.server_task = server_task
        self.futex_task = futex_task
        self.identifier_process = self.server_task.process.near
        self.logger = logging.getLogger(f"rsyscall.ChildConnection.{int(self.server_task.process)}")
        self.infd: handle.FileDescriptor
        self.outfd: handle.FileDescriptor
        self.activity_fd: near.FileDescriptor
        self.request_lock = trio.Lock()
        self.pending_responses: t.List[SyscallResponse] = []
        self.running: trio.Event = None

    def store_remote_side_handles(self, infd: handle.FileDescriptor, outfd: handle.FileDescriptor) -> None:
        # these are needed so that we don't close them with garbage collection
        self.infd = infd
        self.outfd = outfd
        # this is part of the SyscallInterface
        self.activity_fd = infd.near

    async def close_interface(self) -> None:
        await self.rsyscall_connection.close()

    async def _read_syscall_response(self) -> int:
        response: int
        try:
            async with trio.open_nursery() as nursery:
                async def read_response() -> None:
                    nonlocal response
                    response = await self.rsyscall_connection.read_response()
                    self.logger.info("read syscall response")
                    nursery.cancel_scope.cancel()
                async def server_exit() -> None:
                    # meaning the server exited
                    try:
                        self.logger.info("enter server exit")
                        await self.server_task.wait_for_exit()
                    except:
                        self.logger.info("out of server exit")
                        raise
                    raise ChildExit()
                async def futex_exit() -> None:
                    if self.futex_task is not None:
                        # meaning the server called exec or exited; we don't
                        # wait to see which one.
                        try:
                            self.logger.info("enter futex exit")
                            await self.futex_task.wait_for_exit()
                        except:
                            self.logger.info("out of futex exit")
                            raise
                        raise MMRelease()
                nursery.start_soon(read_response)
                nursery.start_soon(server_exit)
                nursery.start_soon(futex_exit)
        finally:
            self.logger.info("out of syscall response nursery")
        raise_if_error(response)
        return response

    async def _process_response_for(self, response: SyscallResponse) -> None:
        try:
            ret = await self._read_syscall_response()
            self.logger.info("returned syscall response")
        except Exception as e:
            response.set_exception(e)
        else:
            response.set_result(ret)

    async def _process_one_response_direct(self) -> None:
        if len(self.pending_responses) == 0:
            raise Exception("somehow we are trying to process a syscall response, when there are no pending syscalls.")
        next = self.pending_responses[0]
        await self._process_response_for(next)
        self.pending_responses = self.pending_responses[1:]

    async def _process_one_response(self) -> None:
        if self.running is not None:
            await self.running.wait()
        else:
            running = trio.Event()
            self.running = running
            try:
                await self._process_one_response_direct()
            finally:
                self.running = None
                running.set()

    async def submit_syscall(self, number, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> SyscallResponse:
        async with self.request_lock:
            log_syscall(self.logger, number, arg1, arg2, arg3, arg4, arg5, arg6)
            await self.rsyscall_connection.write_request(
                number,
                arg1=int(arg1), arg2=int(arg2), arg3=int(arg3),
                arg4=int(arg4), arg5=int(arg5), arg6=int(arg6))
        response = SyscallResponse(self._process_one_response)
        self.pending_responses.append(response)
        return response

    async def syscall(self, number, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> int:
        response = await self.submit_syscall(number, arg1, arg2, arg3, arg4, arg5, arg6)
        try:
            # we must not be interrupted while reading the response - we need to return
            # the response so that our parent can deal with the state change we created.
            with trio.open_cancel_scope(shield=True):
                result = await response.receive()
        except Exception as exn:
            self.logger.debug("%s -> %s", number, exn)
            raise
        else:
            self.logger.debug("%s -> %s", number, result)
            return result


class RsyscallInterface(base.SyscallInterface):
    """An rsyscall connection to a task that is not our child.

    For correctness, we should ensure that we'll get HUP/EOF if the task has
    exited and therefore will never respond. This is most easily achieved by
    making sure that the fds keeping the other end of the RsyscallConnection
    open, are only held by one task, and so will be closed when the task
    exits. Note, though, that that requires that the task be in an unshared file
    descriptor space.

    """
    def __init__(self, rsyscall_connection: RsyscallConnection,
                 # usually the same pid that's inside the namespaces
                 identifier_process: near.Process,
                 activity_fd: near.FileDescriptor) -> None:
        self.rsyscall_connection = rsyscall_connection
        self.logger = logging.getLogger(f"rsyscall.RsyscallConnection.{identifier_process.id}")
        self.identifier_process = identifier_process
        self.activity_fd = activity_fd
        # these are needed so that we don't accidentally close them when doing a do_cloexec_except
        self.infd: handle.FileDescriptor
        self.outfd: handle.FileDescriptor
        self.request_lock = trio.Lock()
        self.pending_responses: t.List[SyscallResponse] = []
        self.running: trio.Event = None

    def store_remote_side_handles(self, infd: handle.FileDescriptor, outfd: handle.FileDescriptor) -> None:
        self.infd = infd
        self.outfd = outfd

    async def close_interface(self) -> None:
        await self.rsyscall_connection.close()

    async def _process_response_for(self, response: SyscallResponse) -> None:
        try:
            ret = await self.rsyscall_connection.read_response()
            raise_if_error(ret)
        except Exception as e:
            response.set_exception(e)
        else:
            response.set_result(ret)

    async def _process_one_response_direct(self) -> None:
        if len(self.pending_responses) == 0:
            raise Exception("somehow we are trying to process a syscall response, when there are no pending syscalls.")
        next = self.pending_responses[0]
        await self._process_response_for(next)
        self.pending_responses = self.pending_responses[1:]

    async def _process_one_response(self) -> None:
        if self.running is not None:
            await self.running.wait()
        else:
            running = trio.Event()
            self.running = running
            try:
                await self._process_one_response_direct()
            finally:
                self.running = None
                running.set()

    async def submit_syscall(self, number, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> SyscallResponse:
        async with self.request_lock:
            log_syscall(self.logger, number, arg1, arg2, arg3, arg4, arg5, arg6)
            await self.rsyscall_connection.write_request(
                number,
                arg1=int(arg1), arg2=int(arg2), arg3=int(arg3),
                arg4=int(arg4), arg5=int(arg5), arg6=int(arg6))
        response = SyscallResponse(self._process_one_response)
        self.pending_responses.append(response)
        return response

    async def syscall(self, number, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> int:
        response = await self.submit_syscall(number, arg1, arg2, arg3, arg4, arg5, arg6)
        try:
            # we must not be interrupted while reading the response - we need to return
            # the response so that our parent can deal with the state change we created.
            with trio.open_cancel_scope(shield=True):
                result = await response.receive()
        except Exception as exn:
            self.logger.debug("%s -> %s", number, exn)
            raise
        else:
            self.logger.debug("%s -> %s", number, result)
            return result

async def call_function(task: Task, stack: BufferedStack, process_resources: ProcessResources,
                        function: FunctionPointer, arg1=0, arg2=0, arg3=0, arg4=0, arg5=0, arg6=0) -> ChildEvent:
    "Calls a C function and waits for it to complete. Returns the ChildEvent that the child thread terminated with."
    stack.align()
    stack.push(process_resources.build_trampoline_stack(function, arg1, arg2, arg3, arg4, arg5, arg6))
    stack_pointer = await stack.flush(task.transport)
    # we directly spawn a thread for the function and wait on it
    pid = await raw_syscall.clone(task.syscall, lib.CLONE_VM|lib.CLONE_FILES, stack_pointer, ptid=None, ctid=None, newtls=None)
    process = base.Process(task.base.pidns, near.Process(pid))
    siginfo = await memsys.waitid(task.syscall, task.transport, task.allocator,
                                  process, W.ALL|W.EXITED)
    struct = ffi.cast('siginfo_t*', ffi.from_buffer(siginfo))
    child_event = ChildEvent.make(ChildCode(struct.si_code),
                                  pid=int(struct.si_pid), uid=int(struct.si_uid),
                                  status=int(struct.si_status))
    return child_event

async def do_cloexec_except(task: Task, process_resources: ProcessResources,
                            excluded_fds: t.Iterable[near.FileDescriptor]) -> None:
    "Close all CLOEXEC file descriptors, except for those in a whitelist. Would be nice to have a syscall for this."
    stack_size = 4096
    async with (await task.mmap(stack_size, memory.ProtFlag.READ|memory.ProtFlag.WRITE, memory.MapFlag.PRIVATE)) as mapping:
        stack = BufferedStack(mapping.pointer + stack_size)
        fd_array = array.array('i', [int(fd) for fd in excluded_fds])
        fd_array_ptr = stack.push(fd_array.tobytes())
        child_event = await call_function(task, stack, process_resources,
                                          process_resources.do_cloexec_func, fd_array_ptr, len(fd_array))
        if not child_event.clean():
            raise Exception("cloexec function child died!", child_event)

async def unshare_files(
        task: Task, monitor: ChildProcessMonitor, process_resources: ProcessResources,
        close_in_old_space: t.List[near.FileDescriptor],
        copy_to_new_space: t.List[near.FileDescriptor],
        going_to_exec: bool,
) -> None:
    serializer = memsys.Serializer()
    fds_ptr = serializer.serialize_data(array.array('i', [int(fd) for fd in close_in_old_space]).tobytes())
    stack_ptr = serializer.serialize_lambda(trampoline_stack_size,
        lambda: process_resources.build_trampoline_stack(process_resources.stop_then_close_func,
                                                         fds_ptr.pointer, len(close_in_old_space)),
                                            # aligned for the stack
                                            alignment=16)
    async with serializer.with_flushed(task.transport, task.allocator):
        closer_task = await monitor.clone(
            lib.CLONE_VM|lib.CLONE_FS|lib.CLONE_FILES|lib.CLONE_IO|lib.CLONE_SIGHAND|lib.CLONE_SYSVSEM|signal.SIGCHLD,
            stack_ptr.pointer)
        event = await closer_task.wait_for_stop_or_exit()
        if event.died():
            raise Exception("stop_then_close task died unexpectedly", event)
        # perform the actual unshare
        await near.unshare(task.base.sysif, near.UnshareFlag.FILES)
        # tell the closer task to close
        await closer_task.send_signal(signal.SIGCONT)
        await closer_task.wait_for_exit()
    # perform a cloexec
    if not going_to_exec:
        await do_cloexec_except(task, process_resources, copy_to_new_space)

class LocalMemoryTransport(base.MemoryTransport):
    "This is a memory transport that only works on local pointers."
    def inherit(self, task: handle.Task) -> LocalMemoryTransport:
        return self

    async def write(self, dest: Pointer, data: bytes) -> None:
        await self.batch_write([(dest, data)])

    async def batch_write(self, ops: t.List[t.Tuple[Pointer, bytes]]) -> None:
        for dest, data in ops:
            src = base.to_local_pointer(data)
            n = len(data)
            base.memcpy(dest, src, n)

    async def read(self, src: Pointer, n: int) -> bytes:
        [data] = await self.batch_read([(src, n)])
        return data

    async def batch_read(self, ops: t.List[t.Tuple[Pointer, int]]) -> t.List[bytes]:
        ret: t.List[bytes] = []
        for src, n in ops:
            buf = bytearray(n)
            dest = base.to_local_pointer(buf)
            base.memcpy(dest, src, n)
            ret.append(bytes(buf))
        return ret

@dataclass
class OneAtATime:
    running: t.Optional[trio.Event] = None

    @contextlib.asynccontextmanager
    async def needs_run(self) -> t.AsyncGenerator[bool, None]:
        if self.running is not None:
            yield False
            await self.running.wait()
        else:
            running = trio.Event()
            self.running = running
            try:
                yield True
            finally:
                self.running = None
                running.set()

@dataclass
class ReadOp:
    src: Pointer
    n: int
    done: t.Optional[bytes] = None

    @property
    def data(self) -> bytes:
        if self.done is None:
            raise Exception("not done yet")
        return self.done

@dataclass
class WriteOp:
    dest: Pointer
    data: bytes
    done: bool = False

    def assert_done(self) -> None:
        if not self.done:
            raise Exception("not done yet")

def merge_adjacent_writes(write_ops: t.List[t.Tuple[Pointer, bytes]]) -> t.List[t.Tuple[Pointer, bytes]]:
    "Note that this is only effective inasmuch as the list is sorted."
    if len(write_ops) == 0:
        return []
    write_ops = sorted(write_ops, key=lambda op: int(op[0]))
    outputs: t.List[t.Tuple[Pointer, bytes]] = []
    last_pointer, last_data = write_ops[0]
    for pointer, data in write_ops[1:]:
        if int(last_pointer + len(last_data)) == int(pointer):
            last_data += data
        elif int(last_pointer + len(last_data)) > int(pointer):
            raise Exception("pointers passed to memcpy are overlapping!")
        else:
            outputs.append((last_pointer, last_data))
            last_pointer, last_data = pointer, data
    outputs.append((last_pointer, last_data))
    return outputs

@dataclass
class SocketMemoryTransport(base.MemoryTransport):
    """This class wraps a pair of connected file descriptors, one of which is in the local address space.

    The task owning the "local" file descriptor is guaranteed to be in the local address space. This
    means Python runtime memory, such as bytes objects, can be written to it without fear.  The
    "remote" file descriptor is somewhere else - possibly in the same task, possibly on some other
    system halfway across the planet.

    This pair can be used through the helper methods on this class, or borrowed for direct use. When
    directly used, care must be taken to ensure that at the end of use, the buffer between the pair
    is empty; otherwise later users will get that stray leftover data when they try to use it.

    """
    local: AsyncFileDescriptor[ReadableWritableFile]
    remote: handle.FileDescriptor
    pending_writes: t.List[WriteOp] = field(default_factory=list)
    running_write: OneAtATime = field(default_factory=OneAtATime)
    pending_reads: t.List[ReadOp] = field(default_factory=list)
    running_read: OneAtATime = field(default_factory=OneAtATime)

    @staticmethod
    def merge_adjacent_reads(read_ops: t.List[ReadOp]) -> t.List[t.Tuple[ReadOp, t.List[ReadOp]]]:
        "Note that this is only effective inasmuch as the list is sorted."
        if len(read_ops) == 0:
            return []
        read_ops = sorted(read_ops, key=lambda op: int(op.src))
        last_op = read_ops[0]
        last_orig_ops = [last_op]
        outputs: t.List[t.Tuple[ReadOp, t.List[ReadOp]]] = []
        for op in read_ops[1:]:
            if int(last_op.src + last_op.n) == int(op.src):
                last_op.n += op.n
                last_orig_ops.append(op)
            elif int(last_op.src + last_op.n) == int(op.src):
                raise Exception("pointers passed to memcpy are overlapping!")
            else:
                outputs.append((last_op, last_orig_ops))
                last_op = op
                last_orig_ops = [op]
        outputs.append((last_op, last_orig_ops))
        return outputs

    @property
    def remote_is_local(self) -> bool:
        return self.remote.task.address_space == base.local_address_space

    def inherit(self, task: handle.Task) -> SocketMemoryTransport:
        return SocketMemoryTransport(self.local, task.make_fd_handle(self.remote))

    async def _unlocked_single_write(self, dest: Pointer, data: bytes) -> None:
        src = base.to_local_pointer(data)
        n = len(data)
        if self.remote_is_local:
            base.memcpy(dest, src, n)
            return
        rtask = self.remote.task
        near_read_fd = self.remote.near
        near_dest = rtask.to_near_pointer(dest)
        wtask = self.local.underlying.task.base
        near_write_fd = self.local.underlying.handle.near
        near_src = wtask.to_near_pointer(src)
        async def read() -> None:
            i = 0
            while (n - i) > 0:
                ret = await near.read(rtask.sysif, near_read_fd, near_dest+i, n-i)
                i += ret
        async def write() -> None:
            i = 0
            while (n - i) > 0:
                ret = await self.local.write_raw(wtask.sysif, near_write_fd, near_src+i, n-i)
                i += ret
        async with trio.open_nursery() as nursery:
            nursery.start_soon(read)
            nursery.start_soon(write)

    async def _unlocked_batch_write(self, ops: t.List[t.Tuple[Pointer, bytes]]) -> None:
        ops = sorted(ops, key=lambda op: int(op[0]))
        ops = merge_adjacent_writes(ops)
        if len(ops) <= 1:
            [(dest, data)] = ops
            await self._unlocked_single_write(dest, data)
        else:
            # TODO use an iovec
            # build the full iovec at the start
            # write it over with unlocked_single_write
            # call readv
            # on partial read, fall back to unlocked_single_write for the rest of that section,
            # then go back to an incremented iovec
            for dest, data in ops:
                await self._unlocked_single_write(dest, data)

    def _start_single_write(self, dest: Pointer, data: bytes) -> WriteOp:
        write = WriteOp(dest, data)
        self.pending_writes.append(write)
        return write

    async def _do_writes(self) -> None:
        async with self.running_write.needs_run() as needs_run:
            if needs_run:
                writes = self.pending_writes
                self.pending_writes = []
                # TODO we should not use a cancel scope shield, we should use the SyscallResponse API
                with trio.open_cancel_scope(shield=True):
                    await self._unlocked_batch_write([(write.dest, write.data) for write in writes])
                for write in writes:
                    write.done = True

    async def write(self, dest: Pointer, data: bytes) -> None:
        await self.batch_write([(dest, data)])

    async def batch_write(self, ops: t.List[t.Tuple[Pointer, bytes]]) -> None:
        write_ops = [self._start_single_write(dest, data) for (dest, data) in ops]
        await self._do_writes()
        for op in write_ops:
            op.assert_done()

    async def _unlocked_single_read(self, src: Pointer, n: int) -> bytes:
        buf = bytearray(n)
        dest = base.to_local_pointer(buf)
        if self.remote_is_local:
            base.memcpy(dest, src, n)
            return bytes(buf)
        rtask = self.local.underlying.task.base
        near_dest = rtask.to_near_pointer(dest)
        near_read_fd = self.local.underlying.handle.near
        wtask = self.remote.task
        near_src = wtask.to_near_pointer(src)
        near_write_fd = self.remote.near
        async def read() -> None:
            i = 0
            while (n - i) > 0:
                ret = await self.local.read_raw(rtask.sysif, near_read_fd, near_dest+i, n-i)
                i += ret
        async def write() -> None:
            i = 0
            while (n - i) > 0:
                ret = await near.write(wtask.sysif, near_write_fd, near_src+i, n-i)
                i += ret
        async with trio.open_nursery() as nursery:
            nursery.start_soon(read)
            nursery.start_soon(write)
        return bytes(buf)

    async def _unlocked_batch_read(self, ops: t.List[ReadOp]) -> None:
        for op in ops:
            op.done = await self._unlocked_single_read(op.src, op.n)

    def _start_single_read(self, dest: Pointer, n: int) -> ReadOp:
        op = ReadOp(dest, n)
        self.pending_reads.append(op)
        return op

    async def _do_reads(self) -> None:
        async with self.running_read.needs_run() as needs_run:
            if needs_run:
                ops = self.pending_reads
                self.pending_reads = []
                merged_ops = self.merge_adjacent_reads(ops)
                # TODO we should not use a cancel scope shield, we should use the SyscallResponse API
                with trio.open_cancel_scope(shield=True):
                    await self._unlocked_batch_read([op for op, _ in merged_ops])
                for op, orig_ops in merged_ops:
                    data = op.data
                    for orig_op in orig_ops:
                        orig_op.done, data = data[:orig_op.n], data[orig_op.n:]

    async def read(self, src: Pointer, n: int) -> bytes:
        [data] = await self.batch_read([(src, n)])
        return data

    async def batch_read(self, ops: t.List[t.Tuple[Pointer, int]]) -> t.List[bytes]:
        read_ops = [self._start_single_read(src, n) for src, n in ops]
        await self._do_reads()
        return [op.data for op in read_ops]

class AsyncReadBuffer:
    def __init__(self, fd: AsyncFileDescriptor[ReadableFile]) -> None:
        self.fd = fd
        self.buf = b""

    async def _read(self) -> t.Optional[bytes]:
        data = await self.fd.read()
        if len(data) == 0:
            if len(self.buf) != 0:
                raise Exception("got EOF while we still hold unhandled buffered data")
            else:
                return None
        else:
            return data

    async def read_length(self, length: int) -> t.Optional[bytes]:
        while len(self.buf) < length:
            data = await self._read()
            if data is None:
                return None
            self.buf += data
        section = self.buf[:length]
        self.buf = self.buf[length:]
        return section

    async def read_cffi(self, name: str) -> t.Any:
        size = ffi.sizeof(name)
        data = await self.read_length(size)
        if data is None:
            raise Exception("got EOF while expecting to read a", name)
        nameptr = name + '*'
        dest = ffi.new(nameptr)
        # ffi.cast drops the reference to the backing buffer, so we have to copy it
        src = ffi.cast(nameptr, ffi.from_buffer(data))
        ffi.memmove(dest, src, size)
        return dest[0]

    async def read_length_prefixed_string(self) -> bytes:
        elem_size = await self.read_cffi('size_t')
        elem = await self.read_length(elem_size)
        if elem is None:
            raise Exception("got EOF while expecting to read environment element of length", elem_size)
        return elem

    async def read_length_prefixed_array(self, length: int) -> t.List[bytes]:
        ret: t.List[bytes] = []
        for _ in range(length):
            ret.append(await self.read_length_prefixed_string())
        return ret

    async def read_envp(self, length: int) -> t.Dict[bytes, bytes]:
        raw = await self.read_length_prefixed_array(length)
        environ: t.Dict[bytes, bytes] = {}
        for elem in raw:
            # if someone passes us a malformed environment element without =,
            # we'll just break, whatever
            key, val = elem.split(b"=", 1)
            environ[key] = val
        return environ

    async def read_until_delimiter(self, delim: bytes) -> t.Optional[bytes]:
        while True:
            try:
                i = self.buf.index(delim)
            except ValueError:
                pass
            else:
                section = self.buf[:i]
                # skip the delimiter
                self.buf = self.buf[i+1:]
                return section
            # buf contains no copies of "delim", gotta read some more data
            data = await self._read()
            if data is None:
                return None
            self.buf += data

    async def read_line(self) -> t.Optional[bytes]:
        return (await self.read_until_delimiter(b"\n"))

    async def read_keyval(self) -> t.Optional[t.Tuple[bytes, bytes]]:
        keyval = await self.read_line()
        if keyval is None:
            return None
        key, val = keyval.split(b"=", 1)
        return key, val

    async def read_known_keyval(self, expected_key: bytes) -> bytes:
        keyval = await self.read_keyval()
        if keyval is None:
            raise Exception("expected key value pair with key", expected_key, "but got EOF instead")
        key, val = keyval
        if key != expected_key:
            raise Exception("expected key", expected_key, "but got", key)
        return val

    async def read_known_int(self, expected_key: bytes) -> int:
        return int(await self.read_known_keyval(expected_key))

    async def read_known_fd(self, expected_key: bytes) -> near.FileDescriptor:
        return near.FileDescriptor(await self.read_known_int(expected_key))

    async def read_netstring(self) -> t.Optional[bytes]:
        length_bytes = await self.read_until_delimiter(b':')
        if length_bytes is None:
            return None
        length = int(length_bytes)
        data = await self.read_length(length)
        if data is None:
            raise Exception("hangup before netstring data")
        comma = await self.read_length(1)        
        if comma is None:
            raise Exception("hangup before comma at end of netstring")
        if comma != b",":
            raise Exception("bad netstring delimiter", comma)
        return data

async def set_singleton_robust_futex(task: far.Task, transport: base.MemoryTransport, allocator: memory.PreallocatedAllocator,
                                     futex_value: int,
) -> Pointer:
    serializer = memsys.Serializer()
    futex_offset = ffi.sizeof('struct robust_list')
    futex_data = struct.pack('=I', futex_value)
    robust_list_entry = serializer.serialize_lambda(
        futex_offset + len(futex_data),
        # we indicate that this is the last entry in the list by pointing it to itself
        lambda: (bytes(ffi.buffer(ffi.new('struct robust_list*', (ffi.cast('void*', robust_list_entry.pointer),))))
                 + futex_data))
    robust_list_head = serializer.serialize_cffi(
        'struct robust_list_head', lambda:
        ((ffi.cast('void*', robust_list_entry.pointer),), futex_offset, ffi.cast('void*', 0)))
    async with serializer.with_flushed(transport, allocator):
        await far.set_robust_list(task, robust_list_head.pointer, robust_list_head.size)
    futex_pointer = robust_list_entry.pointer + futex_offset
    return futex_pointer

async def make_connections(access_task: Task,
                           # regrettably asymmetric...
                           # it would be nice to unify connect/accept with passing file descriptors somehow.
                           access_connection: t.Optional[t.Tuple[Path, FileDescriptor[UnixSocketFile]]],
                           connecting_task: Task,
                           connecting_connection: t.Tuple[handle.FileDescriptor, handle.FileDescriptor],
                           parent_task: Task,
                           count: int) -> t.List[t.Tuple[FileDescriptor[ReadableWritableFile], handle.FileDescriptor]]:
    # so there's 1. the access task, through which we access the syscall and data fds,
    # 2. the parent task, and
    # 3. the connection between the access and parent task, so that we can have the parent task pass down the fds,
    # while the access task uses them.
    # okay but this is a slight simplification, because there may also be,
    # 4. the connection task, which is a task that actually gets the fds and passes them down to the parent task
    access_socks: t.List[FileDescriptor[ReadableWritableFile]] = []
    connecting_socks: t.List[FileDescriptor[ReadableWritableFile]] = []
    if access_task.base.fd_table == connecting_task.base.fd_table:
        async def make_conn() -> t.Tuple[FileDescriptor[ReadableWritableFile], FileDescriptor[ReadableWritableFile]]:
            return (await access_task.socketpair(socket.AF_UNIX, SOCK.STREAM, 0))
    else:
        if access_connection is not None:
            access_connection_path, access_connection_socket = access_connection
        else:
            raise Exception("must pass access connection when access task and connecting task are different")
        async def make_conn() -> t.Tuple[FileDescriptor[ReadableWritableFile], FileDescriptor[ReadableWritableFile]]:
            left_sock = await access_task.socket_unix(SOCK.STREAM)
            await robust_unix_connect(access_connection_path, left_sock)
            right_sock: FileDescriptor[UnixSocketFile]
            right_sock, _ = await access_connection_socket.accept(O.CLOEXEC) # type: ignore
            return left_sock, right_sock
    for _ in range(count):
        access_sock, connecting_sock = await make_conn()
        access_socks.append(access_sock)
        connecting_socks.append(connecting_sock)
    passed_socks: t.List[handle.FileDescriptor]
    if connecting_task.base.fd_table == parent_task.base.fd_table:
        passed_socks = []
        for sock in connecting_socks:
            passed_socks.append(parent_task.base.make_fd_handle(sock.handle))
            await sock.handle.invalidate()
    else:
        assert connecting_connection is not None
        await memsys.sendmsg_fds(connecting_task.base, connecting_task.transport, connecting_task.allocator,
                                 connecting_connection[0].far, [sock.handle.far for sock in connecting_socks])
        near_passed_socks = await memsys.recvmsg_fds(parent_task.base, parent_task.transport, parent_task.allocator,
                                                     connecting_connection[1].far, count)
        passed_socks = [parent_task.base.make_fd_handle(sock) for sock in near_passed_socks]
        # don't need these in the connecting task anymore
        for sock in connecting_socks:
            await sock.aclose()
    ret = list(zip(access_socks, passed_socks))
    return ret

async def spawn_rsyscall_thread(
        access_sock: AsyncFileDescriptor[ReadableWritableFile],
        remote_sock: handle.FileDescriptor,
        parent_task: Task, thread_maker: ThreadMaker, function: FunctionPointer,
        newuser: bool, newpid: bool, fs: bool, sighand: bool,
    ) -> t.Tuple[Task, CThread]:
    flags = lib.CLONE_VM|lib.CLONE_FILES|lib.CLONE_IO|lib.CLONE_SYSVSEM|signal.SIGCHLD
    # TODO correctly track the namespaces we're in for all these things
    if newuser:
        flags |= lib.CLONE_NEWUSER
    if newpid:
        flags |= lib.CLONE_NEWPID
    if fs:
        flags |= lib.CLONE_FS
    if sighand:
        flags |= lib.CLONE_SIGHAND
    cthread = await thread_maker.make_cthread(flags, function, remote_sock.near, remote_sock.near)
    syscall = ChildConnection(
        RsyscallConnection(access_sock, access_sock),
        cthread.child_task,
        cthread.futex_task)
    if fs:
        fs_information = parent_task.base.fs
    else:
        fs_information = far.FSInformation(
            cthread.child_task.process.near.id, root=parent_task.base.fs.root, cwd=parent_task.base.fs.cwd)
    if newpid:
        pidns = far.PidNamespace(cthread.child_task.process.near.id)
    else:
        pidns = parent_task.base.pidns
    netns = parent_task.base.netns
    new_base_task = base.Task(syscall, cthread.child_task.process,
                              parent_task.fd_table, parent_task.address_space, fs_information, pidns, netns)
    remote_sock_handle = new_base_task.make_fd_handle(remote_sock)
    syscall.store_remote_side_handles(remote_sock_handle, remote_sock_handle)
    new_task = Task(new_base_task,
                    # We don't inherit the transport because it leads to a deadlock:
                    # If when a child task calls transport.read, it performs a syscall in the child task,
                    # then the parent task will need to call waitid to monitor the child task during the syscall,
                    # which will in turn need to also call transport.read.
                    # But the child is already using the transport and holding the lock,
                    # so the parent will block forever on taking the lock,
                    # and child's read syscall will never complete.
                    parent_task.transport,
                    parent_task.allocator.inherit(new_base_task),
                    parent_task.sigmask.inherit(),
    )
    return new_task, cthread

async def make_robust_futex_task(
        parent_stdtask: StandardTask,
        parent_memfd: handle.FileDescriptor,
        child_stdtask: StandardTask,
        child_memfd: handle.FileDescriptor,
) -> t.Tuple[ChildProcess, handle.MemoryMapping, handle.MemoryMapping]:
    # resize memfd appropriately
    futex_memfd_size = 4096
    await parent_memfd.ftruncate(futex_memfd_size)
    # set up local mapping
    local_mapping = await parent_memfd.mmap(
        futex_memfd_size, memory.ProtFlag.READ|memory.ProtFlag.WRITE, memory.MapFlag.SHARED)
    await parent_memfd.invalidate()
    local_mapping_pointer = local_mapping.as_pointer()
    # set up remote mapping
    remote_mapping = await child_memfd.mmap(
        futex_memfd_size, memory.ProtFlag.READ|memory.ProtFlag.WRITE, memory.MapFlag.SHARED)
    await child_memfd.invalidate()
    remote_mapping_pointer = remote_mapping.as_pointer()

    # have to set the futex pointer to this nonsense or the kernel won't wake on it properly
    futex_value = FUTEX_WAITERS|(int(child_stdtask.task.base.process) & FUTEX_TID_MASK)
    # this is distasteful and leaky, we're relying on the fact that the PreallocatedAllocator never frees things
    remote_futex_pointer = await set_singleton_robust_futex(
        child_stdtask.task.base, child_stdtask.task.transport,
        memory.PreallocatedAllocator(remote_mapping_pointer, futex_memfd_size), futex_value)
    local_futex_pointer = local_mapping_pointer + (int(remote_futex_pointer) - int(remote_mapping_pointer))
    # now we start the futex monitor
    futex_task = await launch_futex_monitor(parent_stdtask.task.base, parent_stdtask.task.transport,
                                            parent_stdtask.task.allocator,
                                            parent_stdtask.process, parent_stdtask.child_monitor,
                                            local_futex_pointer, futex_value)
    local_mapping_handle = handle.MemoryMapping(parent_stdtask.task.base, local_mapping)
    remote_mapping_handle = handle.MemoryMapping(child_stdtask.task.base, remote_mapping)
    return futex_task, local_mapping_handle, remote_mapping_handle

async def rsyscall_exec(
        parent_stdtask: StandardTask,
        rsyscall_thread: RsyscallThread,
        rsyscall_server_path: handle.Path,
    ) -> None:
    "Exec into the standalone rsyscall_server executable"
    stdtask = rsyscall_thread.stdtask
    [(access_data_sock, passed_data_sock)] = await stdtask.make_async_connections(1)
    # create this guy and pass him down to the new thread
    futex_memfd = await memsys.memfd_create(stdtask.task.base, stdtask.task.transport, stdtask.task.allocator,
                                                  b"child_robust_futex_list", MFD.CLOEXEC)
    child_futex_memfd = stdtask.task.base.make_fd_handle(futex_memfd)
    parent_futex_memfd = parent_stdtask.task.base.make_fd_handle(futex_memfd)
    syscall: ChildConnection = stdtask.task.base.sysif # type: ignore
    def encode(fd: near.FileDescriptor) -> bytes:
        return str(int(fd)).encode()
    async def do_unshare(close_in_old_space: t.List[near.FileDescriptor],
                         copy_to_new_space: t.List[near.FileDescriptor]) -> None:
        # unset cloexec on all the fds we want to copy to the new space
        for copying_fd in copy_to_new_space:
            await near.fcntl(syscall, copying_fd, fcntl.F_SETFD, 0)
        child_task = await rsyscall_thread.execve(
            rsyscall_server_path, [
                b"rsyscall_server",
                encode(passed_data_sock.near), encode(syscall.infd.near), encode(syscall.outfd.near),
                *[encode(fd) for fd in copy_to_new_space],
            ], {}, [stdtask.child_monitor.internal.signal_queue.signal_block])
        #### read symbols from describe fd
        describe_buf = AsyncReadBuffer(access_data_sock)
        symbol_struct = await describe_buf.read_cffi('struct rsyscall_symbol_table')
        stdtask.process = ProcessResources.make_from_symbols(stdtask.task.base.address_space, symbol_struct)
        # the futex task we used before is dead now that we've exec'd, have
        # to null it out
        syscall.futex_task = None
        # TODO maybe remove dependence on parent task for closing?
        for fd in close_in_old_space:
            await near.close(parent_stdtask.task.base.sysif, fd)
        stdtask.task.base.address_space = base.AddressSpace(rsyscall_thread.thread.child_task.process.near.id)
        # we mutate the allocator instead of replacing to so that anything that
        # has stored the allocator continues to work
        stdtask.task.allocator.allocator = memory.Allocator(stdtask.task.base)
        stdtask.task.transport = SocketMemoryTransport(access_data_sock, passed_data_sock)
    await stdtask.task.base.unshare_files(do_unshare)

    #### make new futex task
    futex_task, local_mapping, remote_mapping = await make_robust_futex_task(parent_stdtask, parent_futex_memfd,
                                                                             stdtask, child_futex_memfd)
    syscall.futex_task = futex_task
    # TODO how do we unmap the remote mapping?
    rsyscall_thread.thread = Thread(rsyscall_thread.thread.child_task, futex_task, local_mapping)

# Need to identify the host, I guess
# I shouldn't abstract this too much - I should just use ssh.
@contextlib.asynccontextmanager
async def run_socket_binder(
        task: StandardTask,
        ssh_command: SSHCommand,
) -> t.AsyncGenerator[bytes, None]:
    stdout_pipe = await task.task.pipe()
    async_stdout = await AsyncFileDescriptor.make(task.epoller, stdout_pipe.rfd)
    thread = await task.fork()
    bootstrap_executable = await thread.stdtask.task.open(thread.stdtask.filesystem.rsyscall_bootstrap_path, O.RDONLY)
    stdout = thread.stdtask.task.base.make_fd_handle(stdout_pipe.wfd.handle)
    await stdout_pipe.wfd.handle.invalidate()
    await thread.stdtask.unshare_files()
    # TODO we are relying here on the fact that replace_with doesn't set cloexec on the new fd.
    # maybe we should explicitly list what we want to pass down...
    # or no, let's tag things as inheritable, maybe?
    await thread.stdtask.stdout.replace_with(stdout)
    await thread.stdtask.stdin.replace_with(bootstrap_executable)
    async with thread:
        child = await ssh_command.args(ssh_bootstrap_script_contents).exec(thread)
        # from... local?
        # I guess this throws into sharper relief the distinction between core and module.
        # The ssh bootstrapping stuff should come from a different class,
        # which hardcodes the path,
        # and which works only for local tasks.
        # So in the meantime we'll continue to get it from task.filesystem.

        # sigh, openssh doesn't close its local stdout when it sees HUP/EOF on
        # the remote stdout. so we can't use EOF to signal end of our lines, and
        # instead have to have a sentinel to tell us when to stop reading.
        lines_buf = AsyncReadBuffer(async_stdout)
        tmp_path_bytes = await lines_buf.read_line()
        if tmp_path_bytes is None:
            raise Exception("got EOF from ssh socket binder?")
        done = await lines_buf.read_line()
        if done != b"done":
            raise Exception("socket binder violated protocol, got instead of done:", done)
        await async_stdout.aclose()
        logger.info("socket bootstrap done, got tmp path %s", tmp_path_bytes)
        yield tmp_path_bytes
        (await child.wait_for_exit()).check()

async def ssh_forward(stdtask: StandardTask, ssh_command: SSHCommand,
                      local_path: handle.Path, remote_path: str) -> ChildProcess:
    stdout_pipe = await stdtask.task.pipe()
    async_stdout = await AsyncFileDescriptor.make(stdtask.epoller, stdout_pipe.rfd)
    thread = await stdtask.fork()
    stdout = thread.stdtask.task.base.make_fd_handle(stdout_pipe.wfd.handle)
    await stdout_pipe.wfd.invalidate()
    await thread.stdtask.unshare_files()
    await thread.stdtask.stdout.replace_with(stdout)
    child_task = await ssh_command.local_forward(
        local_path, remote_path,
    ).args("-n", "echo forwarded; sleep inf").exec(thread)
    lines_buf = AsyncReadBuffer(async_stdout)
    forwarded = await lines_buf.read_line()
    if forwarded != b"forwarded":
        raise Exception("ssh forwarding violated protocol, got instead of forwarded:", forwarded)
    await async_stdout.aclose()
    return child_task

async def ssh_bootstrap(
        parent_task: StandardTask,
        # the actual ssh command to run
        ssh_command: SSHCommand,
        # the root directory we'll have on the remote side
        ssh_root: near.DirectoryFile,
        # the local path we'll use for the socket
        local_socket_path: handle.Path,
        # the directory we're bootstrapping out of
        tmp_path_bytes: bytes,
) -> t.Tuple[ChildProcess, StandardTask]:
    # identify local path
    task = parent_task.task
    local_data_path = Path(task, local_socket_path)
    # start port forwarding; we'll just leak this process, no big deal
    forward_child = await ssh_forward(
        parent_task, ssh_command, local_socket_path, (tmp_path_bytes + b"/data").decode())
    # start bootstrap
    bootstrap_thread = await parent_task.fork()
    bootstrap_child_task = await ssh_command.args(
        "-n", f"cd {tmp_path_bytes.decode()}; ./bootstrap rsyscall"
    ).exec(bootstrap_thread)
    # TODO should unlink the bootstrap after I'm done execing.
    # it would be better if sh supported fexecve, then I could unlink it before I exec...
    # Connect to local socket 4 times
    async def make_async_connection() -> AsyncFileDescriptor[UnixSocketFile]:
        sock = await task.socket_unix(SOCK.STREAM)
        await robust_unix_connect(local_data_path, sock)
        return (await AsyncFileDescriptor.make(parent_task.epoller, sock))
    async_local_syscall_sock = await make_async_connection()
    async_local_data_sock = await make_async_connection()
    # Read description off of the data sock
    describe_buf = AsyncReadBuffer(async_local_data_sock)
    describe_struct = await describe_buf.read_cffi('struct rsyscall_bootstrap')
    new_pid = describe_struct.pid
    new_fd_table = far.FDTable(new_pid)
    def to_fd(num: int) -> far.FileDescriptor:
        return far.FileDescriptor(new_fd_table, near.FileDescriptor(num))
    listening_fd = to_fd(describe_struct.listening_sock)
    remote_syscall_fd = to_fd(describe_struct.syscall_sock)
    remote_data_fd = to_fd(describe_struct.data_sock)
    environ = await describe_buf.read_envp(describe_struct.envp_count)
    # Build the new task!
    new_address_space = far.AddressSpace(new_pid)
    # TODO the pid namespace will probably be common for all connections...
    new_pid_namespace = far.PidNamespace(new_pid)
    new_process = far.Process(new_pid_namespace, near.Process(new_pid))
    new_syscall = RsyscallInterface(RsyscallConnection(async_local_syscall_sock, async_local_syscall_sock),
                                    new_process.near, remote_syscall_fd.near)
    # the cwd is not the one from the ssh_host because we cd'd somewhere else as part of the bootstrap
    new_fs_information = far.FSInformation(new_pid, root=ssh_root, cwd=near.DirectoryFile())
    # TODO we should get this from the SSHHost, this is usually going
    # to be common for all connections and we should express that
    net = far.NetNamespace(new_pid)
    new_base_task = base.Task(new_syscall, new_process, new_fd_table, new_address_space, new_fs_information,
                              new_pid_namespace, net)
    handle_remote_syscall_fd = new_base_task.make_fd_handle(remote_syscall_fd)
    new_syscall.store_remote_side_handles(handle_remote_syscall_fd, handle_remote_syscall_fd)
    handle_remote_data_fd = new_base_task.make_fd_handle(remote_data_fd)
    new_transport = SocketMemoryTransport(async_local_data_sock, handle_remote_data_fd)
    new_task = Task(new_base_task, new_transport,
                    memory.AllocatorClient.make_allocator(new_base_task),
                    # we assume ssh zeroes the sigmask before starting us
                    SignalMask(set()),
    )
    left_connecting_connection, right_connecting_connection = await new_task.socketpair(socket.AF_UNIX, SOCK.STREAM, 0)
    connecting_connection = (left_connecting_connection.handle, right_connecting_connection.handle)
    epoller = await new_task.make_epoll_center()
    child_monitor = await ChildProcessMonitor.make(new_task, epoller)
    new_stdtask = StandardTask(
        access_task=parent_task.task,
        access_epoller=parent_task.epoller,
        access_connection=(local_data_path, new_task.make_fd(listening_fd.near, UnixSocketFile())),
        connecting_task=new_task, connecting_connection=connecting_connection,
        task=new_task,
        process_resources=ProcessResources.make_from_symbols(new_address_space, describe_struct.symbols),
        filesystem_resources=FilesystemResources.make_from_environ(new_base_task, environ),
        epoller=epoller,
        child_monitor=child_monitor,
        environment=environ,
        stdin=new_task._make_fd(0, ReadableFile(shared=True)),
        stdout=new_task._make_fd(1, WritableFile(shared=True)),
        stderr=new_task._make_fd(2, WritableFile(shared=True)),
    )
    return bootstrap_child_task, new_stdtask

async def spawn_ssh(
        task: StandardTask,
        ssh_command: SSHCommand,
        ssh_root: near.DirectoryFile,
        local_socket_path: handle.Path,
) -> t.Tuple[ChildProcess, StandardTask]:
    async with run_socket_binder(task, ssh_command) as tmp_path_bytes:
        return (await ssh_bootstrap(task, ssh_command, ssh_root, local_socket_path, tmp_path_bytes))

class SSHHost:
    @abc.abstractmethod
    async def ssh(self, task: StandardTask) -> t.Tuple[ChildProcess, StandardTask]: ...

@dataclass
class ArbitrarySSHHost(SSHHost):
    root: near.DirectoryFile
    command: SSHCommand
    async def ssh(self, task: StandardTask) -> t.Tuple[ChildProcess, StandardTask]:
        # we could get rid of the need to touch the local filesystem by directly
        # speaking the openssh multiplexer protocol. or directly speaking the ssh
        # protocol for that matter.
        random_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        name = (self.guess_hostname()+random_suffix+".sock")
        local_socket_path: handle.Path = task.filesystem.tmpdir/name
        return (await spawn_ssh(task, self.command, self.root, local_socket_path))

    def guess_hostname(self) -> str:
        # we guess that the last argument of ssh command is the hostname. it
        # doesn't matter if it isn't, this is just for human-readability.
        return os.fsdecode(self.command.arguments[-1])

class RsyscallThread:
    def __init__(self,
                 stdtask: StandardTask,
                 thread: Thread,
    ) -> None:
        self.stdtask = stdtask
        self.thread = thread

    async def exec(self, command: Command) -> ChildProcess:
        return (await command.exec(self))

    async def execve(self, path: handle.Path, argv: t.Sequence[t.Union[str, bytes, os.PathLike]],
                     env_updates: t.Mapping[str, t.Union[str, bytes, os.PathLike]]={},
                     inherited_signal_blocks: t.List[SignalBlock]=[],
    ) -> ChildProcess:
        """Replace the running executable in this thread with another.

        We take inherited_signal_blocks as an argument so that we can default it
        to "inheriting" an empty signal mask. Most programs expect the signal
        mask to be cleared on startup. Since we're using signalfd as our signal
        handling method, we need to block signals with the signal mask; and if
        those blocked signals were inherited across exec, other programs would
        break (SIGCHLD is the most obvious example).

        We could depend on the user clearing the signal mask before calling
        exec, similar to how we require the user to remove CLOEXEC from
        inherited fds; but that is a fairly novel requirement to most, so for
        simplicity we just default to clearing the signal mask before exec, and
        allow the user to explicitly pass down additional signal blocks.

        """
        sigmask: t.Set[signal.Signals] = set()
        for block in inherited_signal_blocks:
            sigmask = sigmask.union(block.mask)
        await self.stdtask.task.sigmask.setmask(self.stdtask.task, sigmask)
        envp: t.Dict[bytes, bytes] = {**self.stdtask.environment}
        for key in env_updates:
            envp[os.fsencode(key)] = os.fsencode(env_updates[key])
        raw_envp: t.List[bytes] = []
        for key_bytes, value in envp.items():
            raw_envp.append(b''.join([key_bytes, b'=', value]))
        task = self.stdtask.task
        logger.info("execveat(%s, %s, %s)", path, argv, env_updates)
        return (await self.thread.execveat(task.base.sysif, task.transport, task.allocator,
                                           path, [os.fsencode(arg) for arg in argv],
                                           raw_envp, flags=0))

    async def run(self, command: Command, check=True, *, task_status=trio.TASK_STATUS_IGNORED) -> ChildEvent:
        child = await command.exec(self)
        task_status.started(child)
        exit_event = await child.wait_for_exit()
        if check:
            exit_event.check()
        return exit_event

    async def close(self) -> None:
        await self.thread.close()
        await self.stdtask.task.close()

    async def __aenter__(self) -> StandardTask:
        return self.stdtask

    async def __aexit__(self, *args, **kwargs) -> None:
        await self.close()

class Pipe(t.NamedTuple):
    rfd: FileDescriptor[ReadableFile]
    wfd: FileDescriptor[WritableFile]

    async def aclose(self):
        await self.rfd.aclose()
        await self.wfd.aclose()

    async def __aenter__(self) -> Pipe:
        return self

    async def __aexit__(self, *args, **kwargs):
        await self.aclose()

T_command = t.TypeVar('T_command', bound="Command")
class Command:
    def __init__(self,
                 executable_path: handle.Path,
                 arguments: t.List[t.Union[str, bytes, os.PathLike]],
                 env_updates: t.Mapping[str, t.Union[str, bytes, os.PathLike]]) -> None:
        self.executable_path = executable_path
        self.arguments = arguments
        self.env_updates = env_updates

    def args(self: T_command, *args: t.Union[str, bytes, os.PathLike]) -> T_command:
        return type(self)(self.executable_path,
                          [*self.arguments, *args],
                          self.env_updates)

    def env(self: T_command, env_updates: t.Mapping[str, t.Union[str, bytes, os.PathLike]]={},
            **updates: t.Union[str, bytes, os.PathLike]) -> T_command:
        return type(self)(self.executable_path,
                          self.arguments,
                          {**self.env_updates, **env_updates, **updates})

    def in_shell_form(self) -> str:
        ret = ""
        for key, value in self.env_updates.items():
            ret += os.fsdecode(key) + "=" + os.fsdecode(value)
        ret += os.fsdecode(self.executable_path)
        # skip first argument
        for arg in self.arguments[1:]:
            ret += " " + os.fsdecode(arg)
        return ret

    def __str__(self) -> str:
        ret = "Command("
        for key, value in self.env_updates.items():
            ret += f"{key}={value} "
        ret += f"{os.fsdecode(self.executable_path)},"
        for arg in self.arguments:
            ret += " " + os.fsdecode(arg)
        ret += ")"
        return ret

    # hmm we actually need an rsyscallthread to properly exec
    # would be nice to call this just "Thread".
    # we should namespace the current "Thread" properly, so we can do that...
    async def exec(self, thread: RsyscallThread) -> ChildProcess:
        return (await thread.execve(self.executable_path, self.arguments, self.env_updates))

T_ssh_command = t.TypeVar('T_ssh_command', bound="SSHCommand")
class SSHCommand(Command):
    def ssh_options(self, config: t.Mapping[str, t.Union[str, bytes, os.PathLike]]) -> SSHCommand:
        option_list: t.List[str] = []
        for key, value in config.items():
            option_list += ["-o", os.fsdecode(key) + "=" + os.fsdecode(value)]
        return self.args(*option_list)

    def proxy_command(self, command: Command) -> SSHCommand:
        return self.ssh_options({'ProxyCommand': command.in_shell_form()})

    def local_forward(self, local_socket: handle.Path, remote_socket: str) -> SSHCommand:
        return self.args("-L", os.fsdecode(local_socket) + ":" + os.fsdecode(remote_socket))

    def as_host(self) -> ArbitrarySSHHost:
        return ArbitrarySSHHost(near.DirectoryFile(), self)

    @classmethod
    def make(cls: t.Type[T_ssh_command], executable_path: handle.Path) -> T_ssh_command:
        return cls(executable_path, [b"ssh"], {})

class SSHDCommand(Command):
    def sshd_options(self, config: t.Mapping[str, t.Union[str, bytes, os.PathLike]]) -> SSHDCommand:
        option_list: t.List[str] = []
        for key, value in config.items():
            option_list += ["-o", os.fsdecode(key) + "=" + os.fsdecode(value)]
        return self.args(*option_list)

    @classmethod
    def make(cls, executable_path: handle.Path) -> SSHDCommand:
        return cls(executable_path, [b"sshd"], {})

async def exec_cat(thread: RsyscallThread, cat: Command,
                   infd: handle.FileDescriptor, outfd: handle.FileDescriptor) -> ChildProcess:
    stdin = thread.stdtask.task.base.make_fd_handle(infd)
    stdout = thread.stdtask.task.base.make_fd_handle(outfd)
    await thread.stdtask.unshare_files()
    await thread.stdtask.stdin.replace_with(stdin)
    await thread.stdtask.stdout.replace_with(stdout)
    child_task = await cat.exec(thread)
    return child_task

async def read_all(fd: FileDescriptor[ReadableFile]) -> bytes:
    buf = b""
    while True:
        data = await fd.read()
        if len(data) == 0:
            return buf
        buf += data

async def read_full(read: t.Callable[[int], t.Awaitable[bytes]], size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        buf += await read(size - len(buf))
    return buf

async def bootstrap_nix(
        src_nix_store: Command, src_tar: Command, src_task: StandardTask,
        dest_tar: Command, dest_task: StandardTask, dest_dir: FileDescriptor[DirectoryFile],
) -> t.List[bytes]:
    "Copies the Nix binaries into dest task's CWD. Returns the list of paths in the closure."
    query_thread = await src_task.fork()
    query_pipe = await src_task.task.pipe()
    query_stdout = query_thread.stdtask.task.base.make_fd_handle(query_pipe.wfd.handle)
    await query_pipe.wfd.invalidate()
    await query_thread.stdtask.unshare_files(going_to_exec=True)
    await query_thread.stdtask.stdout.replace_with(query_stdout)
    await src_nix_store.args("--query", "--requisites",
                             src_nix_store.executable_path).exec(query_thread)
    closure = (await read_all(query_pipe.rfd)).split()

    src_tar_thread = await src_task.fork()
    dest_tar_thread = await dest_task.fork()
    [(access_side, dest_tar_stdin)] = await dest_tar_thread.stdtask.make_connections(1)
    src_tar_stdout = access_side.handle.move(src_tar_thread.stdtask.task.base)

    await dest_tar_thread.stdtask.task.unshare_fs()
    await dest_tar_thread.stdtask.task.fchdir(dest_dir.handle)
    await dest_tar_thread.stdtask.unshare_files(going_to_exec=True)
    await dest_tar_thread.stdtask.stdin.replace_with(dest_tar_stdin)
    child_task = await dest_tar.args("--extract").exec(dest_tar_thread)

    await src_tar_thread.stdtask.unshare_files(going_to_exec=True)
    await src_tar_thread.stdtask.stdout.replace_with(src_tar_stdout)
    await src_tar.args(
        "--create", "--to-stdout", "--hard-dereference",
        "--owner=0", "--group=0", "--mode=u+rw,uga+r",
        *closure,
    ).exec(src_tar_thread)
    await child_task.wait_for_exit()
    return closure

async def bootstrap_nix_database(
        src_nix_store: Command, src_task: StandardTask,
        dest_nix_store: Command, dest_task: StandardTask,
        closure: t.List[bytes],
) -> None:
    dump_db_thread = await src_task.fork()
    load_db_thread = await dest_task.fork()
    [(access_side, load_db_stdin)] = await load_db_thread.stdtask.make_connections(1)
    dump_db_stdout = access_side.handle.move(dump_db_thread.stdtask.task.base)

    await load_db_thread.stdtask.unshare_files(going_to_exec=True)
    await load_db_thread.stdtask.stdin.replace_with(load_db_stdin)
    child_task = await dest_nix_store.args("--load-db").env({'NIX_REMOTE': ''}).exec(load_db_thread)

    await dump_db_thread.stdtask.unshare_files(going_to_exec=True)
    await dump_db_thread.stdtask.stdout.replace_with(dump_db_stdout)
    await src_nix_store.args("--dump-db", *closure).exec(dump_db_thread)
    await child_task.check()

async def create_nix_container(
        src_nix_bin: handle.Path, src_task: StandardTask,
        dest_task: StandardTask,
) -> handle.Path:
    dest_nix_bin = dest_task.task.base.make_path_handle(src_nix_bin)
    src_nix_store = Command(src_nix_bin/'nix-store', [b'nix-store'], {})
    dest_nix_store = Command(dest_nix_bin/'nix-store', [b'nix-store'], {})
    # TODO check if dest_nix_bin exists, and skip this stuff if it does
    # copy the nix binaries over
    src_tar = await which(src_task, b"tar")
    dest_tar = await which(dest_task, b"tar")
    closure = await bootstrap_nix(src_nix_store, src_tar, src_task, dest_tar, dest_task) # type: ignore

    # mutate dest_task so that it is nicely namespaced for the Nix container
    await dest_task.unshare_user()
    await dest_task.unshare_mount()
    await dest_task.task.mount(b"nix", b"/nix", b"none", MS.BIND, b"")
    await bootstrap_nix_database(src_nix_store, src_task, dest_nix_store, dest_task, closure)
    return dest_nix_bin

async def deploy_nix_bin(
        src_nix_bin: NixPath, src_tar: Command, src_task: StandardTask,
        deploy_tar: Command, deploy_task: StandardTask,
        dest_task: StandardTask,
) -> handle.Path:
    dest_nix_bin = NixPath(src_nix_bin)
    src_nix_store = Command(src_nix_bin/'nix-store', [b'nix-store'], {})
    dest_nix_store = Command(dest_nix_bin/'nix-store', [b'nix-store'], {})
    # TODO check if dest_nix_bin exists, and skip this stuff if it does
    rootdir = await dest_task.task.root().open_directory()
    closure = await bootstrap_nix(src_nix_store, src_tar, src_task, deploy_tar, deploy_task, rootdir)
    await bootstrap_nix_database(src_nix_store, src_task, dest_nix_store, dest_task, closure)
    return dest_nix_bin

async def nix_deploy(
        src_nix_bin: handle.Path, src_path: handle.Path, src_task: StandardTask,
        dest_nix_bin: handle.Path, dest_task: StandardTask,
) -> handle.Path:
    dest_path = dest_task.task.base.make_path_handle(src_path)

    query_thread = await src_task.fork()
    query_pipe = await src_task.task.pipe()
    query_stdout = query_pipe.wfd.handle.move(query_thread.stdtask.task.base)
    await query_thread.stdtask.unshare_files(going_to_exec=True)
    await query_thread.stdtask.stdout.replace_with(query_stdout)
    await Command(src_nix_bin/"nix-store", [b"nix-store"], {}).args("--query", "--requisites", src_path).exec(query_thread)
    closure = (await read_all(query_pipe.rfd)).split()

    export_thread = await src_task.fork()
    import_thread = await dest_task.fork()
    [(access_side, import_stdin)] = await import_thread.stdtask.make_connections(1)
    export_stdout = access_side.handle.move(export_thread.stdtask.task.base)

    await import_thread.stdtask.unshare_files(going_to_exec=True)
    await import_thread.stdtask.stdin.replace_with(import_stdin)
    child_task = await import_thread.execve(dest_nix_bin/"nix-store", ["nix-store", "--import"])

    await export_thread.stdtask.unshare_files(going_to_exec=True)
    await export_thread.stdtask.stdout.replace_with(export_stdout)
    await export_thread.execve(dest_nix_bin/"nix-store", ["nix-store", "--export", *closure])
    await child_task.check()
    return dest_path

async def canonicalize(task: Task, path: handle.Path) -> Path:
    f = await Path(task, path).open_path()
    return (await Path(task, f.handle.as_proc_path()).readlink())

class NixPath(handle.Path):
    "A path in the Nix store, which can therefore be deployed to a remote host with Nix."
    @classmethod
    async def make(cls, task: Task, path: handle.Path) -> NixPath:
        return cls((await canonicalize(task, path)).handle)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        root, nix, store = self.parts[:3]
        if root != b"/" or nix != b"nix" or store != b"store":
            raise Exception("path doesn't start with /nix/store")

async def read_until_eof(fd: FileDescriptor[ReadableFile]) -> bytes:
    # hmm this is identical to read_all, should delete one of them
    data = b""
    ret = await fd.read()
    while len(ret) != 0:
        data += ret
        ret = await fd.read()
    return data

local_stdtask: StandardTask
async def _initialize_module() -> None:
    global local_stdtask
    local_stdtask = await StandardTask.make_local()
    # wipe out the SIGWINCH handler that the readline module installs
    import readline
    await local_stdtask.task.base.rt_sigaction(
        Signals.SIGWINCH, await local_stdtask.task.to_pointer(Sigaction(Sighandler.DFL)), None)

trio.run(_initialize_module)
