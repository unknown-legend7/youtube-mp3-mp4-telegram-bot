import { api, db, fetch, FormData, InputFile } from 'sdk';
import { sql } from 'sdk/db';
import { imageCallback } from '../lib/images.js';
import { AUDIO, BACKEND, MAX_SIZE, videoChoices } from '../lib/converter.js';

const FAILURE = {
    401: 'Conversion service setup is incomplete.',
    408: 'Upload timed out. Send a smaller clip.',
    413: 'The input or output exceeds 20 MB. Try a shorter clip or lower quality.',
    415: 'This video format is unsupported.',
    422: 'Could not convert this video. It may be damaged, too long, or missing an audio track.',
    429: 'The converter is busy. Send the video again in a minute.',
    503: 'The converter is unavailable or waking up. Try again shortly.',
    504: 'Conversion timed out. Try a shorter clip.',
};

const GUIDES = {
    media: 'Upload a video and choose MP3 or MP4. Maximum 20 MB and 10 minutes.',
    image: 'Upload a JPG, PNG, WebP, BMP or TIFF. Send it as a file to retain the original.',
    compress: 'Upload an image, choose Compress, then select an output format and compression level.',
    pdf: 'Upload one image and choose Image to PDF. For multiple images, send /imagepdf.',
    help: 'Send /help for available tools and limits.',
};

export default async function (query) {
    if (query.data?.startsWith('guide:')) {
        const topic = query.data.slice(6);
        await api.answerCallbackQuery({ callback_query_id: query.id,
            text: Object.hasOwn(GUIDES, topic) ? GUIDES[topic] : GUIDES.help, show_alert: true });
        return;
    }
    if (query.data?.startsWith('i:')) {
        try {
            await imageCallback(query);
        } catch {
            console.error('Image callback failed.');
            await api.answerCallbackQuery({ callback_query_id: query.id, text: 'Image tools are temporarily unavailable.' }).catch(() => {});
        }
        return;
    }
    const match = /^(m|q):(\d+):(mp3|mp4)(?::(\w+))?$/.exec(query.data || '');
    const message = query.message;
    const answer = (text) => api.answerCallbackQuery({ callback_query_id: query.id, text });
    if (!match || !message?.chat || !message.date) {
        await answer('Send the video again to open a new menu.');
        return;
    }

    const [, action, rawId, format, quality] = match;
    const id = Number(rawId);
    const now = Math.floor(Date.now() / 1000);
    const chat_id = message.chat.id;
    const user = query.from.id;
    const edit = (text, reply_markup = { inline_keyboard: [] }) => api.editMessageText({
        chat_id, message_id: message.message_id, text, reply_markup,
    });
    let claimed = false;
    let answered = false;

    try {
        const upload = await db.get(sql`
            SELECT * FROM conversion_uploads WHERE id = ${id} AND chat_id = ${chat_id}
                AND user_id = ${user} AND created_at > ${now - 900}
        `);
        if (!upload || upload.started_at !== null) {
            await answer('This menu is unavailable. Use your own upload or send the video again.');
            return;
        }
        const choices = format === 'mp3' ? AUDIO : videoChoices(upload.short_edge);
        if (action === 'm' && !quality) {
            await answer();
            answered = true;
            await edit(format === 'mp3' ? 'Choose MP3 encoding bitrate.' : 'Choose the maximum MP4 resolution.', {
                inline_keyboard: choices.map((option) => [{
                    text: option === 'source' ? 'Original size (up to 1080p)' :
                        format === 'mp3' ? option.replace('k', ' kbps') : `Up to ${option}`,
                    callback_data: `q:${id}:${format}:${option}`,
                }]),
            });
            return;
        }
        if (action !== 'q' || !choices.includes(quality)) {
            await answer('Choose one of the available qualities.');
            return;
        }

        const setting = await db.get(sql`
            SELECT value FROM converter_settings WHERE name = 'api_key'
        `);
        if (!setting?.value) {
            await answer('Conversion service setup is incomplete.');
            return;
        }
        const result = await db.run(sql`
            UPDATE conversion_uploads SET started_at = ${now}
            WHERE id = ${id} AND started_at IS NULL AND (
                SELECT count(*) FROM conversion_uploads
                WHERE user_id = ${user} AND started_at > ${now - 3600}
            ) < 3 AND NOT EXISTS (
                SELECT 1 FROM conversion_uploads
                WHERE user_id = ${user} AND finished_at IS NULL AND started_at > ${now - 300}
            )
        `);
        if (!result.rowsAffected) {
            await answer('One conversion at a time, up to three attempts per hour.');
            return;
        }
        claimed = true;
        await answer();
        answered = true;
        await edit('Converting your video. The backend may take a moment to wake up.');

        const bytes = await api.getFileContent(upload.file_id);
        if (bytes.byteLength > MAX_SIZE) {
            await edit(FAILURE[413]);
            return;
        }
        const form = new FormData();
        form.append('file', new InputFile(bytes, upload.filename, { type: upload.mime }));
        form.append('output_format', format);
        form.append(format === 'mp3' ? 'audio_bitrate' : 'video_resolution', quality);
        const response = await fetch(BACKEND + '/convert', {
            method: 'POST', headers: { 'x-api-key': setting.value }, body: form,
        });
        if (!response.ok) {
            await edit(FAILURE[response.status] || 'Conversion failed. Send the video again to retry.');
            return;
        }
        if (Number(response.headers.get('content-length')) > MAX_SIZE) {
            await edit(FAILURE[413]);
            return;
        }
        const chunks = [];
        let size = 0;
        for await (const chunk of response.body) {
            size += chunk.byteLength;
            if (size > MAX_SIZE) {
                await edit(FAILURE[413]);
                return;
            }
            chunks.push(chunk);
        }
        const output = new Uint8Array(size);
        let offset = 0;
        for (const chunk of chunks) {
            output.set(chunk, offset);
            offset += chunk.byteLength;
        }
        if (!output.byteLength || output.byteLength > MAX_SIZE) {
            await edit('The converter returned an invalid file. Try a shorter clip.');
            return;
        }

        if (format === 'mp3') {
            await api.sendAudio({ chat_id, audio: new InputFile(output, 'converted.mp3', { type: 'audio/mpeg' }) });
        } else {
            await api.sendVideo({
                chat_id, supports_streaming: true,
                video: new InputFile(output, 'converted.mp4', { type: 'video/mp4' }),
            });
        }
        await edit('Conversion complete.').catch(() => {});
    } catch {
        console.error('Conversion workflow failed.');
        if (claimed) {
            await edit('Conversion or delivery failed. If the backend was asleep, wait a minute and send the video again.');
        } else if (!answered) {
            await answer('The converter is temporarily unavailable.');
        }
    } finally {
        if (claimed) {
            const finished = Math.floor(Date.now() / 1000);
            await db.run(sql`UPDATE conversion_uploads SET finished_at = ${finished} WHERE id = ${id}`);
        }
    }
}
