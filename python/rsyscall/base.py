from __future__ import annotations
from rsyscall._raw import ffi, lib # type: ignore
import os
import typing as t
import logging
import abc
import socket
import struct
import enum
import signal
import ipaddress
from rsyscall.far import AddressSpace, FDTable, Pointer
from rsyscall.far import Process, ProcessGroup, FileDescriptor
from rsyscall.handle import Task
import rsyscall.handle
from rsyscall.near import SyscallInterface
from rsyscall.exceptions import RsyscallException, RsyscallHangup
import rsyscall.far
import rsyscall.near

# re-exported
from rsyscall.memint import MemoryWriter, MemoryReader, MemoryGateway

class MemoryTransport(MemoryGateway):
    @abc.abstractmethod
    def inherit(self, task: Task) -> MemoryTransport: ...

class MemoryAccess:
    # hmm so what exactly should we return here?
    # I guess, sure, a pointer.
    @abc.abstractmethod
    async def to_pointer(self, data: bytes) -> Pointer: ...
    @abc.abstractmethod
    async def malloc(self, n: int) -> Pointer: ...
    @abc.abstractmethod
    async def read(self, ptr: Pointer) -> bytes: ...
