# Frontend developer preview

The Quiet TV Home interface (`frontend/` in the repository) is a React + Vite app. You can run it in a desktop
browser to read, change and test the UI. **This is a developer preview, not an installer.** Nothing here roots,
flashes or installs anything on a TV, and it is not a universal package for arbitrary TVs.

## Run it

Needs Node.js with npm.

```
git clone https://github.com/BDAN13LS/QuietTV.git
cd QuietTV/frontend
npm ci              # exact versions from package-lock.json
npm run dev         # http://127.0.0.1:4176 (local only)
npm test            # unit tests (node --test)
npm run build       # static bundle in frontend/dist
```

Use the arrow keys and Enter to move around, as a TV remote would.

## What the preview does not do

- It does not talk to a TV. Status, launch and capture need the TV-side service, which this repository does not
  ship in installable form; use `public-llm` against a TV that already runs it.
- App tiles cannot open real TV apps in a browser.
- Deploying to a TV needs the device's signing material and root access. Neither is published, and doing it can
  damage or void the warranty of a TV you own. The verified device is one UN43U8000FBXZA on software 1300.8.
- Video playback, DRM, HDMI input and sound are not represented by the preview.

Bug reports and UI changes go through the normal pull-request flow (`community/CONTRIBUTING.md`). UI changes
that are meant to ship to the TV are device-affecting and need the hardware gate.
