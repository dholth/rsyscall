"#include <sys/syscall.h>"
from rsyscall._raw import ffi, lib # type: ignore
import enum

class SYS(enum.IntEnum):
    """The syscall number argument passed to the low-level `syscall` method and underlying instruction

    Passing one of these numbers is how a userspace program indicates to the kernel which
    syscall it wants to call.

    """
    accept4 = lib.SYS_accept4
    bind = lib.SYS_bind
    capget = lib.SYS_capget
    capset = lib.SYS_capset
    chdir = lib.SYS_chdir
    clone = lib.SYS_clone
    close = lib.SYS_close
    connect = lib.SYS_connect
    dup3 = lib.SYS_dup3
    epoll_create1 = lib.SYS_epoll_create1
    epoll_ctl = lib.SYS_epoll_ctl
    epoll_wait = lib.SYS_epoll_wait
    execveat = lib.SYS_execveat
    exit = lib.SYS_exit
    faccessat = lib.SYS_faccessat
    fchdir = lib.SYS_fchdir
    fchmod = lib.SYS_fchmod
    fcntl = lib.SYS_fcntl
    ftruncate = lib.SYS_ftruncate
    getdents64 = lib.SYS_getdents64
    getgid = lib.SYS_getgid
    getpeername = lib.SYS_getpeername
    getpgid = lib.SYS_getpgid
    getsockname = lib.SYS_getsockname
    getsockopt = lib.SYS_getsockopt
    getuid = lib.SYS_getuid
    inotify_add_watch = lib.SYS_inotify_add_watch
    inotify_init1 = lib.SYS_inotify_init1
    inotify_rm_watch = lib.SYS_inotify_rm_watch
    ioctl = lib.SYS_ioctl
    kill = lib.SYS_kill
    linkat = lib.SYS_linkat
    listen = lib.SYS_listen
    lseek = lib.SYS_lseek
    memfd_create = lib.SYS_memfd_create
    mkdirat = lib.SYS_mkdirat
    mmap = lib.SYS_mmap
    mount = lib.SYS_mount
    munmap = lib.SYS_munmap
    openat = lib.SYS_openat
    pipe2 = lib.SYS_pipe2
    prctl = lib.SYS_prctl
    pread64 = lib.SYS_pread64
    preadv2 = lib.SYS_preadv2
    pwritev2 = lib.SYS_pwritev2
    read = lib.SYS_read
    readlinkat = lib.SYS_readlinkat
    recvfrom = lib.SYS_recvfrom
    recvmsg = lib.SYS_recvmsg
    renameat2 = lib.SYS_renameat2
    rt_sigaction = lib.SYS_rt_sigaction
    rt_sigprocmask = lib.SYS_rt_sigprocmask
    sendmsg = lib.SYS_sendmsg
    set_robust_list = lib.SYS_set_robust_list
    set_tid_address = lib.SYS_set_tid_address
    setns = lib.SYS_setns
    setpgid = lib.SYS_setpgid
    setsid = lib.SYS_setsid
    setsockopt = lib.SYS_setsockopt
    shutdown = lib.SYS_shutdown
    signalfd4 = lib.SYS_signalfd4
    socket = lib.SYS_socket
    socketpair = lib.SYS_socketpair
    symlinkat = lib.SYS_symlinkat
    unlinkat = lib.SYS_unlinkat
    unshare = lib.SYS_unshare
    waitid = lib.SYS_waitid
    write = lib.SYS_write
