# Session-string Telegram upload delivery

For a task using the personal Telegram session string:

1. The session string uploads the file to the selected Telegram upload destination.
2. The main bot (`TgClient.bot`) copies the uploaded message to the requesting user's bot DM.
3. The main bot also copies it to the user's configured additional dump chats and task destinations (`-up` / `-ud`).
4. FFmpeg preset destinations (`key_dump_dests`, selected by `-ff`) are collected separately and copied as well; they are deduplicated against the general dump destinations.
5. After the bot-authored replacement exists, the original session-authored message is deleted using the session string first, with bot deletion as a fallback. If neither account has delete permission, the original can remain and a warning is logged.

The session account is used for the upload itself; the bot account handles the final Bot DM and dump copies. The bot must be started by the user to deliver a Bot DM, and must have access/permissions in each configured destination.
