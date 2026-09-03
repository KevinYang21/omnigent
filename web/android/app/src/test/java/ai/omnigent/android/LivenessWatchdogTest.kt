package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class LivenessWatchdogTest {
    @Test
    fun `pre-protocol web disables liveness and negotiated heartbeat loss times out`() {
        val scheduler = FakeScheduler()
        var failures = 0
        val watchdog = LivenessWatchdog(scheduler, onTimeout = { failures++ })

        watchdog.beginDocument()
        watchdog.setActive(false)
        watchdog.setActive(true)
        watchdog.setOnPinnedOrigin(false)
        watchdog.setOnPinnedOrigin(true)
        scheduler.advanceBy(60_000)
        assertEquals(0, failures)
        assertEquals(null, scheduler.remainingOrNull())

        watchdog.protocolReady(1, 1)
        scheduler.advanceBy(14_999)
        assertEquals(0, failures)
        scheduler.advanceBy(1)
        assertEquals(1, failures)
    }

    @Test
    fun `protocol mismatch fails immediately`() {
        val scheduler = FakeScheduler()
        var incompatibilities = 0
        val watchdog = LivenessWatchdog(scheduler, onTimeout = {}) { incompatibilities++ }

        watchdog.beginDocument()
        assertFalse(watchdog.protocolReady(2, 1))
        assertEquals(1, incompatibilities)
        assertEquals(null, scheduler.remainingOrNull())
    }

    @Test
    fun `resume and auth return preserve compatibility through grace with heartbeats`() {
        val scheduler = FakeScheduler()
        var failures = 0
        val watchdog = LivenessWatchdog(scheduler, onTimeout = { failures++ })

        watchdog.beginDocument()
        watchdog.protocolReady(1, 1)
        watchdog.setActive(false)
        scheduler.advanceBy(60_000)
        assertEquals(0, failures)
        watchdog.setActive(true)
        assertEquals(LivenessWatchdog.REACTIVATION_GRACE_MS, scheduler.remaining())
        scheduler.advanceBy(14_000)
        watchdog.heartbeat()
        scheduler.advanceBy(14_000)
        assertEquals(0, failures)

        watchdog.setOnPinnedOrigin(false)
        scheduler.advanceBy(60_000)
        assertEquals(0, failures)
        watchdog.setOnPinnedOrigin(true)
        assertEquals(LivenessWatchdog.REACTIVATION_GRACE_MS, scheduler.remaining())
        scheduler.advanceBy(14_000)
        watchdog.heartbeat()
        scheduler.advanceBy(14_000)
        assertEquals(0, failures)
    }

    @Test
    fun `new document disables liveness until readiness is negotiated again`() {
        val scheduler = FakeScheduler()
        var failures = 0
        val watchdog = LivenessWatchdog(scheduler, onTimeout = { failures++ })

        watchdog.beginDocument()
        watchdog.protocolReady(1, 1)
        watchdog.beginDocument()
        scheduler.advanceBy(60_000)
        watchdog.heartbeat()
        assertEquals(0, failures)
        assertEquals(null, scheduler.remainingOrNull())

        watchdog.protocolReady(1, 1)
        scheduler.advanceBy(LivenessWatchdog.HEARTBEAT_TIMEOUT_MS)
        assertEquals(1, failures)
    }

    private class FakeScheduler : WatchdogScheduler {
        private var now = 0L
        private var due: Long? = null
        private var action: (() -> Unit)? = null

        override fun schedule(
            delayMs: Long,
            action: () -> Unit,
        ) {
            due = now + delayMs
            this.action = action
        }

        override fun cancel() {
            due = null
            action = null
        }

        fun advanceBy(ms: Long) {
            now += ms
            if (due?.let { now >= it } == true) {
                val pending = action
                cancel()
                pending?.invoke()
            }
        }

        fun remaining(): Long = requireNotNull(due) - now

        fun remainingOrNull(): Long? = due?.minus(now)
    }
}
