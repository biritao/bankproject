# AGENTS

This repository contains a Streamlit banking demo application.

Guidelines for AI agents:

- Keep credentials out of source code; prefer environment variables.
- Do not execute destructive SQL statements unless explicitly requested.
- Use stored procedures for posting financial transactions and interest.
- Maintain draft-first transaction flow before booking.
- Keep changes small and testable.
- Run lint and basic app startup checks after major edits.
