from rsyscall.trio_test_case import TrioTestCase
import rsyscall.tasks.local as local
from rsyscall.tests.utils import do_async_things
from rsyscall.epoller import Epoller
from rsyscall.monitor import AsyncSignalfd

from rsyscall.signal import SIG, Sigset
from rsyscall.sys.signalfd import SignalfdSiginfo

class TestFork(TrioTestCase):
    async def asyncSetUp(self) -> None:
        self.local = local.thread
        self.thr = await self.local.fork()

    async def asyncTearDown(self) -> None:
        await self.thr.close()

    async def test_exit(self) -> None:
        await self.thr.exit(0)

    async def test_nest_exit(self) -> None:
        thread = await self.thr.fork()
        async with thread:
            await thread.exit(0)

    async def test_async(self) -> None:
        epoller = await Epoller.make_root(self.thr.ram, self.thr.task)
        await do_async_things(self, epoller, self.thr)

    async def test_nest_async(self) -> None:
        thread = await self.thr.fork()
        async with thread:
            epoller = await Epoller.make_root(thread.ram, thread.task)
            await do_async_things(self, epoller, thread)

    async def test_unshare_async(self) -> None:
        await self.thr.unshare_files()
        thread = await self.thr.fork()
        async with thread:
            epoller = await Epoller.make_root(thread.ram, thread.task)
            await thread.unshare_files()
            await do_async_things(self, epoller, thread)

    async def test_exec(self) -> None:
        child = await self.thr.exec(self.thr.environ.sh.args('-c', 'true'))
        await child.check()

    async def test_mkdtemp(self) -> None:
        async with (await self.thr.mkdtemp()):
            pass

    async def test_signal_queue(self) -> None:
        # have to use an epoller for this specific task
        epoller = await Epoller.make_root(self.thr.ram, self.thr.task)
        sigfd = await AsyncSignalfd.make(self.thr.ram, self.thr.task, epoller, Sigset({SIG.INT}))
        await self.thr.task.process.kill(SIG.INT)
        buf = await self.thr.ram.malloc(SignalfdSiginfo)
        sigdata, _ = await sigfd.afd.read(buf)
        self.assertEqual((await sigdata.read()).signo, SIG.INT)
