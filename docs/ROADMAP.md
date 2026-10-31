# QuietTV roadmap

Checked items have evidence on the reference TV or in the published tooling.
Unchecked items are remaining work, not advertised capabilities or release dates.
The verified profile remains Samsung UN43U8000FBXZA / firmware 1300.8 / Tizen 9 / ARMv7.

## Verified foundations

- [x] Custom Home screen and remote navigation on the reference TV.
- [x] Native app launch, Settings, input picker and Store opening.
- [x] Unattended recovery of the reference TV's existing control service.
- [x] Reference-TV cleanup of 27 optional payload folders, with off-TV owner backups.
- [x] Local MCP/CLI status, controls, screenshots and optional Wake-on-LAN.
- [x] Real package archive and security-metadata capture with TV/PC digest checks.
- [x] PC capsule assembly that streams archive bytes, with integrity and memory regression tests.
- [x] Official Samsung firmware/model/country probe verified on the reference TV, with hashed identity output.
- [x] Offline joining of saved ProductInfo and native reports into exact profile and recovery matching, with forged-receipt refusals.
- [x] Optional Helper display of matched saved reports, with source labels, stale-result invalidation and all TV-changing actions still blocked.
- [x] Windows certificate bootstrap over pinned Apps2Samsung CLI, with existing-owner profile reuse verified; fresh Samsung sign-in remains unverified.
- [x] Fresh report collection with the reviewed public owner-configured provider, verified cleanup, and all seven facts displayed by the running Helper.
- [x] Public `owner-sdk-root` provider end-to-end on the reference TV with a per-owner config: fresh ProductInfo and native frame, seven joined facts, exact Helper config, own-package removal and independent cleanup readback; 570.579 s (~9 min 30 s) from 17 isolated root probes.
- [x] AI setup prompt and client configuration guides; Codex and Opus 4.8 have assisted project work.

These checks do not establish a full stock-TV installation or a complete live restore.

## Consumer-release priorities

- [ ] Integrate observed device/firmware facts into guided setup and exact profile matching; never infer a match from Unknown.
- [x] Publish the owner-configured provider and per-owner setup documentation (manual, no default target or credentials).
- [ ] Complete restore support for the loader references used by Tizen packages.
- [ ] Verify full off-TV backup restoration and independent Rescue on hardware.
- [ ] Connect a published live-collection provider to the Helper guided flow.
- [ ] Finish the Windows Helper, per-owner signing and guided installation.
- [ ] Add reviewed component selection and individual/full restore controls.
- [ ] Enable consumer installation and cleanup only after backup/recovery gates pass.

## Verification and upcoming improvements

- [ ] Verify an actual Store app-update transaction.
- [ ] Verify DRM playback and physical HDMI video.
- [ ] Fix the IPTV black-picture issue and verify picture, sound and channel switching.
- [ ] Improve the remaining cold-start visual handoff.
- [ ] Add browser-assisted setup; standalone stock-TV URL installation remains unproven.
- [ ] Expand widget and layout customization beyond the existing appearance/weather options.
- [ ] Establish repeatable TV RAM/performance measurements; storage reclaimed is not RAM saved.
- [ ] Stream the separate bundle-export path and test large/adversarial metadata trees.
- [ ] Review further cleanup candidates only after dependency and update behavior is established.

## Contribute

Pick a focused unchecked item and open a pull request with tests and appropriate
hardware evidence. See [CONTRIBUTING.md](../CONTRIBUTING.md).
Existing implementation notes are in the [installation plan](CONSUMER-INSTALL-DESIGN-2026-10-27.md),
[capture documentation](../public-toolkit/docs/D2-CAPTURE.md),
[device-facts join](../public-toolkit/docs/FACTS-JOIN.md), and the
[Rescue guide](../rescue/README.md).
