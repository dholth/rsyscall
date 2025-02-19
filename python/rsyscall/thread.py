"Central thread class, with helper methods to ease a number of common tasks"
from __future__ import annotations
from dataclasses import dataclass
from rsyscall.command import Command
from rsyscall.handle import FileDescriptor, Path, WrittenPointer, Pointer, Task
from rsyscall.memory.ram import RAM, RAMThread
from rsyscall.mktemp import mkdtemp, TemporaryDirectory
from rsyscall.unix_thread import UnixThread, ChildUnixThread
import os
import rsyscall.near.types as near
import rsyscall.near as syscalls
import trio
import typing as t

from rsyscall.fcntl import O, F, FD_CLOEXEC
from rsyscall.linux.dirent import DirentList
from rsyscall.sched import CLONE
from rsyscall.sys.mount import MS
from rsyscall.sys.wait import ChildState, W
from rsyscall.unistd import Arg

async def write_user_mappings(thr: RAMThread, uid: int, gid: int,
                              in_namespace_uid: int=None, in_namespace_gid: int=None) -> None:
    """Set up a new user namespace with single-user {uid,gid}_map

    These are the only valid mappings for unprivileged user namespaces.
    """
    if in_namespace_uid is None:
        in_namespace_uid = uid
    if in_namespace_gid is None:
        in_namespace_gid = gid
    procself = Path("/proc/self")

    uid_map = await thr.task.open(await thr.ram.ptr(procself/"uid_map"), O.WRONLY)
    await uid_map.write(await thr.ram.ptr(f"{in_namespace_uid} {uid} 1\n".encode()))
    await uid_map.close()

    setgroups = await thr.task.open(await thr.ram.ptr(procself/"setgroups"), O.WRONLY)
    await setgroups.write(await thr.ram.ptr(b"deny"))
    await setgroups.close()

    gid_map = await thr.task.open(await thr.ram.ptr(procself/"gid_map"), O.WRONLY)
    await gid_map.write(await thr.ram.ptr(f"{in_namespace_gid} {gid} 1\n".encode()))
    await gid_map.close()

async def do_cloexec_except(thr: RAMThread, excluded_fds: t.Set[near.FileDescriptor]) -> None:
    "Close all CLOEXEC file descriptors, except for those in a whitelist. Would be nice to have a syscall for this."
    buf = await thr.ram.malloc(DirentList, 4096)
    dirfd = await thr.task.open(await thr.ram.ptr(Path("/proc/self/fd")), O.DIRECTORY)
    async def maybe_close(fd: near.FileDescriptor) -> None:
        flags = await syscalls.fcntl(thr.task.sysif, fd, F.GETFD)
        if (flags & FD_CLOEXEC) and (fd not in excluded_fds):
            await syscalls.close(thr.task.sysif, fd)
    async with trio.open_nursery() as nursery:
        while True:
            valid, rest = await dirfd.getdents(buf)
            if valid.size() == 0:
                break
            dents = await valid.read()
            for dent in dents:
                try:
                    num = int(dent.name)
                except ValueError:
                    continue
                nursery.start_soon(maybe_close, near.FileDescriptor(num))
            buf = valid.merge(rest)

class Thread(UnixThread):
    "A central class holding everything necessary to work with some thread, along with various helpers"
    async def mkdtemp(self, prefix: str="mkdtemp") -> TemporaryDirectory:
        "Make a temporary directory by calling rsyscall.mktemp.mkdtemp"
        return await mkdtemp(self, prefix)

    @t.overload
    async def spit(self, path: FileDescriptor, text: t.Union[str, bytes]) -> None:
        pass

    @t.overload
    async def spit(self, path: Path, text: t.Union[str, bytes], mode=0o644) -> Path:
        pass

    async def spit(self, path: t.Union[Path, FileDescriptor], text: t.Union[str, bytes], mode=0o644) -> t.Optional[Path]:
        """Open a file, creating and truncating it, and write the passed text to it

        Probably shouldn't use this on FIFOs or anything.

        Returns the passed-in Path so this serves as a nice pseudo-constructor.

        """
        if isinstance(path, Path):
            out: t.Optional[Path] = path
            fd = await self.task.open(await self.ram.ptr(path), O.WRONLY|O.TRUNC|O.CREAT, mode=mode)
        else:
            out = None
            fd = path
        to_write: Pointer = await self.ram.ptr(os.fsencode(text))
        while to_write.size() > 0:
            _, to_write = await fd.write(to_write)
        await fd.close()
        return out

    async def mkdir(self, path: Path, mode=0o755) -> Path:
        "Make a directory at this path"
        await self.task.mkdir(await self.ram.ptr(path))
        return path

    async def read_to_eof(self, fd: FileDescriptor) -> bytes:
        "Read this file descriptor until we get EOF, then return all the bytes read"
        data = b""
        while True:
            read, rest = await fd.read(await self.ram.malloc(bytes, 4096))
            if read.size() == 0:
                return data
            # TODO this would be more efficient if we batched our memory-reads at the end
            data += await read.read()

    async def mount(self, source: bytes, target: bytes,
                    filesystemtype: bytes, mountflags: MS,
                    data: bytes) -> None:
        "Call mount with these args"
        async def op(sem: RAM) -> t.Tuple[
                WrittenPointer[Arg], WrittenPointer[Arg], WrittenPointer[Arg], WrittenPointer[Arg]]:
            return (
                await sem.ptr(Arg(source)),
                await sem.ptr(Arg(target)),
                await sem.ptr(Arg(filesystemtype)),
                await sem.ptr(Arg(data)),
            )
        source_ptr, target_ptr, filesystemtype_ptr, data_ptr = await self.ram.perform_batch(op)
        await self.task.mount(source_ptr, target_ptr, filesystemtype_ptr, mountflags, data_ptr)

    async def fork(self, flags: CLONE=CLONE.NONE) -> ChildThread:
        "Create a new child thread"
        thread = await super().fork(flags)
        if flags & CLONE.NEWUSER:
            # hack, we should really track the [ug]id ahead of this so we don't have to get it
            # we have to get the [ug]id from the parent because it will fail in the child
            uid = await self.task.getuid()
            gid = await self.task.getgid()
            await write_user_mappings(thread, uid, gid)
        return ChildThread(thread, thread.process)

    async def run(self, command: Command, check=True,
                  *, task_status=trio.TASK_STATUS_IGNORED) -> ChildState:
        """Run the passed command to completion and return its end state, throwing if unclean

        If check is False, we won't throw if the end state is unclean.

        """
        thread = await self.fork()
        child = await thread.exec(command)
        task_status.started(child)
        exit_state = await child.waitpid(W.EXITED)
        if check:
            exit_state.check()
        return exit_state

    async def unshare(self, flags: CLONE) -> None:
        "Call the unshare syscall, appropriately updating values on this class"
        # Note: unsharing NEWPID causes us to not get zombies for our children if init dies. That
        # means we'll get ECHILDs, and various races can happen. It's not possible to robustly
        # unshare NEWPID.
        if flags & CLONE.FILES:
            await self.unshare_files()
            flags ^= CLONE.FILES
        if flags & CLONE.NEWUSER:
            await self.unshare_user()
            flags ^= CLONE.NEWUSER
            if flags & CLONE.FS:
                flags ^= CLONE.FS
        await self.task.unshare(flags)

    async def unshare_files(self, going_to_exec=True) -> None:
        """Unshare the file descriptor table.

        Set going_to_exec to False if you are going to keep this task around long-term, and we'll do
        a manual cloexec in userspace to clear out fds held by any other non-rsyscall libraries,
        which are automatically copied by Linux into the new fd space.

        We default going_to_exec to True because there's little reason to call unshare_files other
        than to then exec; and even if you do want to call unshare_files without execing, there
        probably aren't any significant other libraries in the FD space; and even if there are such
        libraries, it usually doesn't matter to keep stray references around to their FDs.

        TODO maybe this should return an object that lets us unset CLOEXEC on things?

        """
        await self.task.unshare_files()
        if not going_to_exec:
            await do_cloexec_except(self, set([fd.near for fd in self.task.fd_handles]))

    async def unshare_files_and_replace(self, mapping: t.Dict[FileDescriptor, FileDescriptor],
                                        disable_cloexec: t.List[FileDescriptor]=[]) -> None:
        """Unshare the file descriptor table, replacing some file descriptors according to the arguments

        This is a useful helper method. `mapping` is a map from the fd to replace, to the fd to
        replace it with.  disable_cloexec is just a list of fds to disable cloexec on, so that they
        will be inherited without changing fd number.

        """
        mapping = {
            # we maybe_copy the key because we need to have the only handle to it in the task,
            # which we'll then consume through dup3.
            key.maybe_copy(self.task):
            # we for_task the value so that we get a copy of it, which we then explicitly invalidate;
            # this means if we had the only reference to the fd passed into us as an expression,
            # we will close that fd - nice.
            val.for_task(self.task)
            for key, val in mapping.items()}
        disable_cloexec = [fd.maybe_copy(self.task) for fd in disable_cloexec]
        await self.unshare_files()
        for dest, source in mapping.items():
            await source.dup3(dest, 0)
            await source.invalidate()
        for fd in disable_cloexec:
            await fd.disable_cloexec()

    async def unshare_user(self,
                           in_namespace_uid: int=None, in_namespace_gid: int=None) -> None:
        """Unshare the user namespace.

        We automatically set up the user namespace in the unprivileged way using a single user
        mapping line.  You can pass `in_namespace_uid` and `in_namespace_gid` to control what user
        id and group id we'll observe inside the namespace.  If you want further control, call
        task.unshare(CLONE.NEWUSER) directly.

        We also automatically do unshare(CLONE.FS); that's required by CLONE.NEWUSER.

        """
        uid = await self.task.getuid()
        gid = await self.task.getgid()
        await self.task.unshare(CLONE.FS|CLONE.NEWUSER)
        await write_user_mappings(self, uid, gid,
                                  in_namespace_uid=in_namespace_uid, in_namespace_gid=in_namespace_gid)

    async def exit(self, status: int) -> None:
        """Exit this thread

        Currently we just forward through to exit the task.

        I feel suspicious that this method will at some point require more heavy lifting with
        namespaces and monitored children, so I'm leaving it on Thread to prepare for that
        eventuality.

        """
        await self.task.exit(status)

    async def close(self) -> None:
        """Close this thread

        Currently we just forward through to close the task.

        """
        await self.task.close_task()

    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, *args, **kwargs):
        await self.close()

class ChildThread(Thread, ChildUnixThread):
    "A thread that we know is also a direct child process of another thread"
    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, *args, **kwargs) -> None:
        await self.close()
