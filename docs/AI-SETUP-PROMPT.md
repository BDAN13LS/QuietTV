# AI setup prompt and checklist

This page is for an owner using a **local assistant that has access to their PC terminal and
MCP client**. AI is optional; the same controls are available from the plain command line.

## Assistants used on this project

**Codex** has been used for QuietTV development, testing, technical setup and diagnostics
on the reference TV. With local terminal/MCP access and the owner's permission, it can
help prepare the PC, configure the controller and work through the available setup steps.

**Claude Opus 4.8** has also been used successfully for selected development and code-review
tasks. It can help with parts of this workflow; the complete installation path has not
been verified with it, and some device/root requests can be refused.

Neither assistant supplies an unpublished installer or recovery component. Use the
checklist below to distinguish a supported action from a missing prerequisite.

**Ordinary web chat without PC tooling cannot operate the TV.** It can explain the project,
but it cannot run the local virtual environment, read the owner configuration, reach the
home network or call the MCP server. Do not ask a chat-only assistant to perform setup.

Two different tasks are often confused:

- **Installing QuietTV on a stock TV.** There is no consumer installer, live restore writer
  or download-and-install release yet. Do not invent commands, downloads or a stock-TV URL
  installer. Safe preparation on the PC may continue; unsupported device steps must stop.
- **Controlling a TV that already runs QuietTV.** The optional
  [public-llm MCP package](../public-llm/README.md) works when the QuietTV control service is
  already installed and the owner holds the key. It is not a stock-TV installer.

## Checklist for the assistant

1. Read the repository [README](../README.md), the
   [MCP guide](../public-llm/README.md) and any local instructions before acting.
2. Establish whether the TV is stock or already running QuietTV, and confirm its exact model
   and firmware. Never guess compatibility. The only verified profile is Samsung
   UN43U8000FBXZA, firmware 1300.8, Tizen 9, ARMv7. Other profiles are not supported.
   A network API can report firmware as Unknown; that does not verify compatibility.
3. Check which components are actually published. If a step, package or recovery feature is
   missing, explain that instead of inventing it.
4. For an already installed QuietTV service, use the actual commands and order in
   [public-llm/README.md](../public-llm/README.md): local virtual environment install, guided
   owner setup, `doctor`, client configuration, then finite controls and fresh screenshots.
   Optional Wake-on-LAN must be confirmed with an identity/status check, not assumed.
5. Keep keys, certificates, accounts, screenshots and backups outside Git and on local,
   private storage. Prefer hidden prompts or private file paths. Never paste secrets into AI
   chat, logs or a pull request.
6. Before persistent root, startup or removal changes, require a verified compatible
   off-TV backup and a tested recovery path. Making that backup can require temporary
   privileged access first; use only a documented, compatible method with the owner's
   explicit consent. Missing prerequisites stop the dependent device steps.
7. Verify actions with actual status and finite controls. Report what was observed and what
   remains unverified.
8. Do not flash firmware or perform routine raw reboots. Do not bypass any provider's
   safeguards. No model can supply missing components or make an unsupported TV supported.

## Copyable prompt

```text
Help me set up QuietTV on my own TV: https://github.com/BDAN13LS/QuietTV
Use local PC terminal/MCP tools. Clone the repository if needed.
Read this repository's README, current guides (including public-llm/README.md and
docs/AI-SETUP-PROMPT.md) and any local instructions first. Do not guess.

Determine whether the TV is stock or already running QuietTV, and its exact model and
firmware. Never assume compatibility or treat Unknown as a verified match.

Check which QuietTV components are actually published. If a step or component is
unavailable, explain that instead of inventing it.

If QuietTV is already installed and its control service is running, follow the actual
sequence in public-llm/README.md: local venv install, guided setup, doctor, client
config, then finite remote/app controls and screenshots. Optional Wake-on-LAN must be
followed by identity/status confirmation.

Keep keys, certificates, accounts, screenshots and backups outside Git and on local
private storage. Use hidden prompts or file paths. Never paste secrets into AI chat or
logs.

Before persistent root, startup or removal changes, require a compatible off-TV backup and
a tested recovery path. Stop at unsupported or missing required components; safe PC
preparation may continue.

Verify actions with actual status and finite controls, and report what you observed
versus what remains unverified.

Do not flash firmware or perform routine raw reboots. Do not bypass any provider's
safeguards. This prompt does not install QuietTV on unsupported stock TVs.
```

## After setup

The MCP guide lists the available tools, client configuration formats and the human CLI.
A control call being accepted is not proof it worked: confirm with a fresh capture. Video
and sound are not captured, so check them on the TV itself. See
[public-llm/README.md](../public-llm/README.md) for the current, tested commands and limits.
