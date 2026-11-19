import { api, db, fetch } from 'sdk';
import { sql } from 'sdk/db';
import { BACKEND, MAX_SIZE } from '../lib/converter.js';

const HELP = 'Send a video you own or have permission to convert (up to 20 MB and 10 minutes). Choose MP3 or MP4, then pick a quality. YouTube links are currently unavailable.';

export default async function (message) {
    const chat_id = message.chat.id;
    const send = (text, extra = {}) => api.sendMessage({ chat_id, text, ...extra });
    const text = (message.text || '').trim();
    const command = text.split(/[@\s]/, 1)[0];

    if (command === '/start' || command === '/help') {
        await send(HELP);
        return;
    }

    if (command === '/backend' || command === '/status') {
        try {
            const response = await fetch(BACKEND + '/health');
            const healthy = response.ok && (await response.json()).status === 'healthy';
            await send(healthy ? 'Render backend is online.' : 'Render backend is unavailable.');
        } catch {
            await send('Render backend may be waking up. Try again shortly.');
        }
        return;
    }

    const file = message.video || message.document;
    if (!file || !message.from || message.sender_chat) {
        await send(HELP);
        return;
    }

    const mime = file.mime_type || (message.video ? 'video/mp4' : 'application/octet-stream');
    const filename = file.file_name || 'video.mp4';
    if (!/\.(mp4|mov|mkv|webm)$/i.test(filename) ||
        !(mime.startsWith('video/') || mime === 'application/octet-stream')) {
        await send('Send an MP4, MOV, MKV or WebM video.');
        return;
    }
    if (file.file_size > MAX_SIZE || file.duration > 600) {
        await send('The limit is 20 MB and 10 minutes per video.');
        return;
    }

    try {
        const now = Math.floor(Date.now() / 1000);
        const user = message.from.id;
        const shortEdge = Math.min(file.width || 0, file.height || 0);
        await db.run(sql`DELETE FROM conversion_uploads WHERE created_at < ${now - 7200}`);
        const result = await db.run(sql`
            INSERT INTO conversion_uploads
                (chat_id, user_id, message_id, file_id, filename, mime, short_edge, created_at)
            SELECT ${chat_id}, ${user}, ${message.message_id}, ${file.file_id}, ${filename}, ${mime}, ${shortEdge}, ${now}
            WHERE NOT EXISTS (
                SELECT 1 FROM conversion_uploads WHERE chat_id = ${chat_id} AND message_id = ${message.message_id}
            ) AND (
                SELECT count(*) FROM conversion_uploads WHERE user_id = ${user} AND created_at > ${now - 300}
            ) < 20
        `);
        if (!result.rowsAffected) {
            await send('This upload already has a menu, or you have sent too many videos. Try again in a few minutes.');
            return;
        }
        const id = result.lastInsertRowid;
        await send('Choose output format.', {
            reply_markup: { inline_keyboard: [[
                { text: 'Convert to MP3', callback_data: `m:${id}:mp3` },
                { text: 'Convert to MP4', callback_data: `m:${id}:mp4` },
            ]] },
        });
    } catch {
        console.error('Could not register conversion upload.');
        await send('The converter is temporarily unavailable.');
    }
}
