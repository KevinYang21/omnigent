package ai.omnigent.android

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * The server-picker payload baked into the bridge facade: shape, ordering, and
 * the JS-literal safety of the embedded JSON.
 */
@RunWith(RobolectricTestRunner::class)
class NativeBridgeScriptTest {
    @Test
    fun `picker payload carries origin and offered servers in order`() {
        val json =
            NativeBridgeScript.serverPickerJson(
                currentOrigin = "https://corp.example.com",
                servers = listOf("https://corp.example.com", "https://mine.example.com"),
            )

        val parsed = JSONObject(json)
        assertEquals("https://corp.example.com", parsed.getString("currentOrigin"))
        val servers = parsed.getJSONArray("recentServers")
        assertEquals(2, servers.length())
        assertEquals("https://corp.example.com", servers.getString(0))
        assertEquals("https://mine.example.com", servers.getString(1))
    }

    @Test
    fun `picker payload escapes JS line separators`() {
        // U+2028/U+2029 are legal in JSON but terminate a JS line — unescaped
        // they would break the injected script.
        val json =
            NativeBridgeScript.serverPickerJson(
                currentOrigin = "https://corp.example.com",
                servers = listOf("https://weird.example.com/\u2028\u2029"),
            )

        assertFalse(json.contains('\u2028'))
        assertFalse(json.contains('\u2029'))
        assertTrue(json.contains("\\u2028"))
        assertTrue(json.contains("\\u2029"))
    }

    @Test
    fun `bridge facade exposes the server picker trio with the payload baked in`() {
        val payload =
            NativeBridgeScript.serverPickerJson(
                currentOrigin = "https://corp.example.com",
                servers = listOf("https://mine.example.com"),
            )
        val script = NativeBridgeScript.source(payload)

        assertTrue(script.contains("getServerPicker()"))
        assertTrue(script.contains("Promise.resolve($payload)"))
        assertTrue(script.contains("""post({ method: "switchServer", url })"""))
        assertTrue(script.contains("""post({ method: "openServerSetup" })"""))
    }
}
