from __future__ import annotations
from rsyscall._raw import lib, ffi # type: ignore
import enum
import typing as t
from rsyscall.struct import Serializable
if t.TYPE_CHECKING:
    from rsyscall.handle import Pointer, WrittenPointer
else:
    Pointer = object

class RWF(enum.IntFlag):
    NONE = 0
    DSYNC = lib.RWF_DSYNC
    HIPRI = lib.RWF_HIPRI
    SYNC = lib.RWF_SYNC

class IovecList(t.List[Pointer], Serializable):
    def split(self, n: int) -> t.Tuple[IovecList, IovecList]:
        first, middle, second = self.split_with_middle(n)
        if middle is None:
            return first, second
        else:
            first_mid, second_mid = middle
            return IovecList(first + [first_mid]), IovecList([second_mid] + second)

    def split_with_middle(self, n: int) -> t.Tuple[IovecList, t.Optional[t.Tuple[Pointer, Pointer]], IovecList]:
        valid: t.List[Pointer] = []
        middle: t.Optional[t.Tuple[Pointer, Pointer]] = None
        invalid: t.List[Pointer] = []
        for ptr in self:
            size = ptr.size()
            if n >= size:
                valid.append(ptr)
                n -= size
            elif n > 0:
                middle = ptr.split(n)
                n = 0
            else:
                invalid.append(ptr)
        return IovecList(valid), middle, IovecList(invalid)

    def to_bytes(self) -> bytes:
        ret = b""
        for ptr in self:
            ret += bytes(ffi.buffer(ffi.new('struct iovec const*', {
                "iov_base": ffi.cast('void*', int(ptr.near)),
                "iov_len": ptr.size(),
            })))
        return ret

    T = t.TypeVar('T', bound='IovecList')
    @classmethod
    def from_bytes(cls: t.Type[T], data: bytes) -> T:
        raise Exception("can't get pointer handles from raw bytes")

def split_iovec(iov: WrittenPointer[IovecList], ret: int
) -> t.Tuple[WrittenPointer[IovecList], t.Optional[t.Tuple[Pointer, Pointer]], WrittenPointer[IovecList]]:
    first, middle, last = iov.value.split_with_middle(ret)
    if middle is None:
        first_count = len(first)
    else:
        # we include the partially-consumed middle pointer in the first WP[IovecList] returned;
        # I think this is the most ergonomic choice, since in the normal case, I'll want to
        # operate on middle separately, then on second.
        first_count = len(first) + 1
    # TODO this is fairly ad-hoc, splitting on a WrittenPointer should really call into the
    # Serializer to determine validity. (And then I suppose we would have .degrade to degrade a
    # WrittenPointer back into a Pointer so we can split it??? Hmm, seems awkward...)
    first_ptr, last_ptr = iov.split(first_count * ffi.sizeof('struct iovec'))
    return first_ptr._wrote(first), middle, last_ptr._wrote(last)

