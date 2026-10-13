import asyncio
import unittest

from services.jobs import BackgroundJobs
from services.telegram_rate import background_traffic


class BackgroundPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_uses_background_priority_without_changing_menu_context(self):
        jobs = BackgroundJobs()
        observed = []

        async def work():
            observed.append(background_traffic.get())
            await asyncio.sleep(0)
            return "ready"

        try:
            self.assertFalse(background_traffic.get())
            self.assertEqual(await jobs.start(42, work, "audit"), "ready")
            self.assertEqual(observed, [True])
            self.assertFalse(background_traffic.get())
            await asyncio.sleep(0)
            self.assertFalse(jobs.active(42))
        finally:
            await jobs.close()

    async def test_cancelled_job_releases_slot_and_context(self):
        jobs = BackgroundJobs()
        entered = asyncio.Event()

        async def work():
            self.assertTrue(background_traffic.get())
            entered.set()
            await asyncio.Event().wait()

        try:
            task = jobs.start(42, work, "audit")
            await entered.wait()
            self.assertTrue(jobs.cancel(42))
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            self.assertFalse(jobs.active(42))
            self.assertFalse(background_traffic.get())
        finally:
            await jobs.close()
