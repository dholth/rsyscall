class MemoryAccess:
    @abc.abstractmethod
    async def to_allocation(self, data: bytes) -> t.Tuple[MemoryMapping, AllocationInterface]: ...

    def to_pointer(self, data: T_has_serializer, alignment: int=1) -> WrittenPointer[T_has_serializer]:
        serializer = data.get_serializer(self.task)
        data_bytes = serializer.to_bytes(data)
        mapping, allocation = await self.to_allocation(data_bytes)
        return WrittenPointer(mapping, self, data, serializer, allocation)

    # well sometimes we do want to override the allocator though.
    # we did this for set singleton robust futex,
    # and we do it for um, something else.
    # what's up with that?
    @abc.abstractmethod
    def bulk_malloc(self, n: t.List[int]) -> t.Tuple[MemoryMapping, AllocationInterface]: ...

    async def malloc(self, size: int, alignment: int) -> t.Tuple[MemoryMapping, Allocation]:
        [ret] = await self.bulk_malloc([(size, alignment)])
        return ret

    async def malloc_serializer(self, serializer: Serializer[T], size: int) -> handle.Pointer[T]:
        mapping, allocation = await self.allocator.malloc(size, alignment=alignment)
        return handle.Pointer(mapping, self, serializer, allocation)

    def malloc_type(self, cls: t.Type[T_has_serializer], size: int) -> Pointer[T_has_serializer]:
        return self.malloc_serializer(cls.get_serializer(self.task), size)

    def malloc_struct(self, cls: t.Type[T_fixed_size]) -> Pointer[T_fixed_size]:
        return self.malloc_type(cls, cls.sizeof())

    @abc.abstractmethod
    async def batch_write(self, ops: t.List[t.Tuple[Pointer, bytes]]) -> None: ...

    async def write(self, dest: Pointer, data: bytes) -> None:
        await self.batch_write([(dest, data)])

    @abc.abstractmethod
    async def batch_read(self, ops: t.List[t.Tuple[Pointer, bytes]]) -> None: ...

    async def read(self, src: Pointer, n: int) -> bytes:
        [data] = await self.batch_read([(src, n)])
        return data
