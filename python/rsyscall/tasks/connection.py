"""The rsyscall protocol implementation

That's a bit grandiose, because the rsyscall protocol is extremely simple: Write
a syscall request out as a fixed-size struct containing a syscall number and the
arguments, read the syscall response in as a 64-bit long.

Still, it's not completely trivial, because we do pipelining of syscall
requests, and we also batch together multiple requests so they can be written
out all at once.

"""
from rsyscall._raw import ffi # type: ignore
from dataclasses import dataclass
from rsyscall.handle import Pointer, Task
from rsyscall.concurrency import OneAtATime
from rsyscall.struct import T_fixed_size, Struct, Int32, StructList
from rsyscall.epoller import AsyncFileDescriptor
from rsyscall.near.sysif import SyscallHangup
import typing as t
import trio

__all__ = [
    "SyscallConnection",
    "ConnectionResponse",
    "Syscall",
]

class ConnectionError(Exception):
    "Something has gone wrong with the rsyscall connection"
    pass

@dataclass
class Syscall(Struct):
    "The struct representing a syscall request"
    number: int
    arg1: int
    arg2: int
    arg3: int
    arg4: int
    arg5: int
    arg6: int

    def to_bytes(self) -> bytes:
        return bytes(ffi.buffer(ffi.new('struct rsyscall_syscall const*', {
            "sys": self.number,
            "args": (self.arg1, self.arg2, self.arg3, self.arg4, self.arg5, self.arg6),
        })))

    T = t.TypeVar('T', bound='Syscall')
    @classmethod
    def from_bytes(cls: t.Type[T], data: bytes) -> T:
        struct = ffi.cast('struct rsyscall_syscall*', ffi.from_buffer(data))
        return cls(struct.sys,
                   struct.args[0], struct.args[1], struct.args[2],
                   struct.args[3], struct.args[4], struct.args[5])

    @classmethod
    def sizeof(cls) -> int:
        return ffi.sizeof('struct rsyscall_syscall')

@dataclass
class SyscallResponse(Struct):
    "The struct representing a syscall response"
    value: int

    def to_bytes(self) -> bytes:
        return bytes(ffi.buffer(ffi.new('long const*', self.value)))

    T = t.TypeVar('T', bound='SyscallResponse')
    @classmethod
    def from_bytes(cls: t.Type[T], data: bytes) -> T:
        struct = ffi.cast('long*', ffi.from_buffer(data))
        return cls(struct[0])

    @classmethod
    def sizeof(cls) -> int:
        return ffi.sizeof('long')

@dataclass
class ConnectionResponse:
    "The mutable object that will eventually contain the decoded syscall return value"
    result: t.Optional[int] = None

@dataclass
class ConnectionRequest:
    syscall: Syscall
    response: t.Optional[ConnectionResponse] = None

class ReadBuffer:
    "A simple buffer for deserializing structs"
    def __init__(self, task: Task) -> None:
        # To read and write structures, we have to know what task
        # they're coming from.
        self.task = task
        self.buf = b""

    def feed_bytes(self, data: bytes) -> None:
        self.buf += data

    def read_struct(self, cls: t.Type[T_fixed_size]) -> t.Optional[T_fixed_size]:
        "Read one fixed-size struct from the buffer, or return None if that's not possible"
        length = cls.sizeof()
        if length <= len(self.buf):
            section = self.buf[:length]
            self.buf = self.buf[length:]
            return cls.get_serializer(self.task).from_bytes(section)
        else:
            return None

    def read_all_structs(self, cls: t.Type[T_fixed_size]) -> t.List[T_fixed_size]:
        "Read as many fixed-size structs from the buffer as possible"
        ret: t.List[T_fixed_size] = []
        while True:
            x = self.read_struct(cls)
            if x is None:
                return ret
            ret.append(x)

class SyscallConnection:
    "A connection to some rsyscall server where we can make syscalls"
    def __init__(self,
                 tofd: AsyncFileDescriptor,
                 fromfd: AsyncFileDescriptor,
    ) -> None:
        self.tofd = tofd
        self.fromfd = fromfd
        self.buffer = ReadBuffer(self.fromfd.handle.task)
        self.valid: t.Optional[Pointer[bytes]] = None
        self.sending_requests = OneAtATime()
        self.pending_requests: t.List[ConnectionRequest] = []
        self.reading_responses = OneAtATime()
        self.pending_responses: t.List[ConnectionResponse] = []

    async def close(self) -> None:
        "Close this SyscallConnection; will throw if there are pending requests"
        if self.pending_requests:
            # TODO we might want to do this, maybe we could cancel these instead?
            # note that we don't check responses - exit, for example, doesn't get a response...
            # TODO maybe we should cancel the response when we detect death of task in the enclosing classes?
            raise Exception("can't close while there are pending requests", self.pending_requests)
        await self.tofd.close()
        await self.fromfd.close()

    async def write_request(self, syscall: Syscall) -> ConnectionResponse:
        """Write a syscall request, returning a ConnectionResponse

        The ConnectionResponse will eventually have .result set to contain the
        syscall return value; you can call read_pending_responses to do work on
        the connection until that happens.

        """
        request = ConnectionRequest(syscall)
        self.pending_requests.append(request)
        # TODO as a hack, so we don't have to figure it out now, we don't allow
        # a syscall request to be cancelled before it's actually made. we could
        # make this work later, and that would reduce some blocking from waitid
        with trio.CancelScope(shield=True):
            while request.response is None:
                await self._write_pending_requests()
        return request.response

    async def read_pending_responses(self) -> None:
        "Process some syscall responses, setting their values on the appropriate ConnectionResponse"
        async with self.reading_responses.needs_run() as needs_run:
            if needs_run:
                await self._read_pending_responses_direct()

    async def _read_pending_responses_direct(self) -> None:
        vals = self.buffer.read_all_structs(SyscallResponse)
        if vals:
            self._got_responses(vals)
            return
        buf = await self.fromfd.ram.malloc(bytes, 1024)
        while not vals:
            if self.valid is None:
                valid, rest = await self.fromfd.read(buf)
                if valid.size() == 0:
                    raise SyscallHangup()
                self.valid = valid
            data = await self.valid.read()
            self.valid = None
            self.buffer.feed_bytes(data)
            buf = valid.merge(rest)
            vals = self.buffer.read_all_structs(SyscallResponse)
        self._got_responses(vals)

    def _got_responses(self, vals: t.List[SyscallResponse]) -> None:
        responses = self.pending_responses[:len(vals)]
        self.pending_responses = self.pending_responses[len(vals):]
        for response, val in zip(responses, vals):
            response.result = val.value

    async def _write_pending_requests(self) -> None:
        "Batch together all pending requests and write them out"
        async with self.sending_requests.needs_run() as needs_run:
            if needs_run:
                await self._write_pending_requests_direct()

    async def _write_pending_requests_direct(self) -> None:
        requests = self.pending_requests
        self.pending_requests = []
        syscalls = StructList(Syscall, [request.syscall for request in requests])
        try:
            ptr = await self.tofd.ram.ptr(syscalls)
            # TODO should mark the requests complete incrementally as we write them out,
            # instead of only once all requests have been written out
            to_write: Pointer = ptr
            while to_write.size() > 0:
                written, to_write = await self.tofd.write(to_write)
        except OSError as e:
            # we raise a different exception so that users can distinguish syscall errors from
            # transport errors
            # TODO we should copy the exception to all the requesters,
            # not just the one calling us; otherwise they'll block forever.
            raise ConnectionError() from e
        # set the response field on the requests to indicate that they've been written
        responses = [ConnectionResponse() for _ in requests]
        for request, response in zip(requests, responses):
            request.response = response
        self.pending_responses += responses
