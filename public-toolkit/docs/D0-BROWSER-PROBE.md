# TV browser setup-page proof — October 27, 2026

The stock browser on the test UN43U8000FBXZA, firmware1300.8, Tizen9,
opened a harmless HTTP page served by a PC on the same home network.
Its user agent identified SamsungBrowser8 and Chromium120.

The page fetched a same-origin JSON response and posted a small JSON echo.
The server observed successful responses for both requests from the TV;
a fresh compositor capture independently showed `Roundtrip: OK` and the
expected echo. No cross-origin exception or browser security setting was
needed. Home then returned to QuietTV and directional navigation worked.

This establishes a **LAN page and data-exchange path** for a future Helper
setup screen. The test TV already had QuietTV and root access. This is
**not evidence of installing, signing or acquiring root from a URL on a
stock TV**. The Windows Helper remains the first planned installation path.

Downloads, uploading a backup off the TV, browser-only root/bootstrap, and
the complete consumer setup flow were not tested. An HTTP download request
alone would not prove a recovery backup was safely saved somewhere else.

The diagnostic listener offered only the page, a read-only probe, a bounded
JSON echo and a fixed synthetic text sample. It had no installation, shell,
proxy, upload or filesystem-write operation. It used an explicit private
LAN bind, exact Host/Origin checks and bounded requests. Parent-only device
diagnostics were removed afterwards and the listener stopped. Boot,
recovery-helper and native-launcher hashes remained unchanged. The raw
screen captures and device session data stay private.

This diagnostic source is not included in this developer-preview export.
There is no public browser installer yet.
