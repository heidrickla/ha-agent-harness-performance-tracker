# Security

Agent Harness Performance Tracker receives run records through a Home Assistant action and a webhook, keeps them in Home Assistant's own storage and derives figures from them. It contacts no service. Its attack surface is the webhook, what it puts in a diagnostics download, and what a reporter can make it store.

## Reporting a vulnerability

Do not open a public issue. Use GitHub's private vulnerability reporting on this repository: the Security tab, then Report a vulnerability. An acknowledgement follows when the report is read.

Include the integration version from `manifest.json`, the Home Assistant version, and what you did.

## What counts as a security issue

| Case | Why |
|---|---|
| The webhook id reaching an entity attribute, a log line, an event or a diagnostics download | The id is the reporter's only credential. It is shown on the confirmation screen and on the agent's Configure screen, which only administrators can open, and `diagnostics.py` redacts it. |
| A run's `notes` reaching a diagnostics download unredacted | Notes are free text a reporter may fill with anything, and a download is routinely pasted into a public issue. |
| A crafted webhook body driving the handler or the coordinator into an unhandled exception | A bad body has to be refused with a 400 naming the field, not take the event loop with it. |
| The webhook accepting a method other than GET and POST, or a POST body that is not an object | Both are refused before validation. GET answers the agent's settings (name, program, file list, label), never a run or a credential. |
| The reporter reading a credential, transcript or history file into a harness version | Settings files are hashed on named harness keys; MCP env and header values are dropped; credential and history files are never selected. |

A command that fails safely, raising an error and writing nothing, is an ordinary bug for the public issue tracker.

## Supported versions

The newest release receives fixes. Earlier ones do not.

## Scope

Home Assistant's own authentication, and the reachability of the webhook endpoint from outside your network, are outside this project. Report the former to [home-assistant/core](https://github.com/home-assistant/core/security/policy).
