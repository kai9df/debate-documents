# Debate Documents

Automatically sends college debate emails and speech documents from Gmail to Discord.

Each email chain gets its own Discord forum post. Replies and attachments appear in the same post, so friends can follow a round and download the evidence.

## What it does

- Uploads the original Word documents and other attachments.
- Keeps different rounds organized in separate posts.
- Runs in Google Cloud, even when your computer is off.

## Setup

You need a dedicated Gmail account, a Discord **Forum** channel with a webhook, and a Google Cloud project with billing enabled.

Follow the [setup guide](SETUP.md) to connect everything. Google Cloud costs depend on usage.

## Using it

1. Add the dedicated Gmail address to a round's email chain.
2. Keep that address included in replies.
3. Open the round's post in Discord to view the messages and documents.

Use a private forum for your group. Never commit passwords, webhook URLs, or Google credentials to this repository.
