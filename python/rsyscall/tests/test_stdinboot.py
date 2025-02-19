from __future__ import annotations

from rsyscall.trio_test_case import TrioTestCase
from rsyscall.nix import local_store
from rsyscall.tasks.stdin_bootstrap import *

import rsyscall.tasks.local as local

from rsyscall.tests.utils import do_async_things
from rsyscall.command import Command

class TestStdinboot(TrioTestCase):
    async def asyncSetUp(self) -> None:
        self.local = local.thread
        path = await stdin_bootstrap_path_from_store(local_store)
        self.command = Command(path, ['rsyscall-stdin-bootstrap'], {})
        self.local_child, self.remote = await stdin_bootstrap(self.local, self.command)

    async def asyncTearDown(self) -> None:
        await self.local_child.kill()

    async def test_exit(self) -> None:
        await self.remote.exit(0)

    async def test_async(self) -> None:
        await do_async_things(self, self.remote.epoller, self.remote)

    async def test_nest(self) -> None:
        child, new_thread = await stdin_bootstrap(self.remote, self.command)
        async with child:
            await do_async_things(self, new_thread.epoller, new_thread)
    
