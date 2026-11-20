import { api, db, fetch, FormData, InputFile } from 'sdk';
import { sql } from 'sdk/db';
import { BACKEND, MAX_SIZE } from './converter.js';

const MIME = { jpg: 'image/jpeg', png: 'image/png', webp: 'image/webp', pdf: 'application/pdf' };
const IMAGE = /\.(jpe?g|png|webp|bmp|tiff?)$/i;
const BATCH = 'image-pdf-batch';
const ACTIONS = {
    png: 'PNG', jpg: 'JPEG', webp: 'WebP',
    compress: 'Compress', resize: 'Resize', pdf: 'Image to PDF', cancel: 'Cancel',
};
const QUALITY = { high: 95, balanced: 82, max: 55 };
const PRESETS = { small: [640, 480], hd: [1280, 720], square: [1080, 1080] };

function clock() {
    return Math.floor(Date.now() / 1000);
}

function keyboard(id, entries, action) {
    return { inline_keyboard: entries.map(([value, text]) => [{
        text, callback_data: action ? `i:${id}:${action}:${value}` : `i:${id}:${value}`,
    }]) };
}

function uploadedImage(message) {
    const photo = message.photo?.[message.photo.length - 1];
    if (photo) return { ...photo, filename: 'photo.jpg', mime: 'image/jpeg' };
    const doc = message.document;
    if (!doc || !IMAGE.test(doc.file_name || '')) return null;
    return { ...doc, filename: doc.file_name, mime: doc.mime_type || 'application/octet-stream' };
}

async function lookup(id, chat, user) {
    const now = clock();
    return db.get(sql`
        SELECT * FROM conversion_uploads WHERE id = ${id} AND chat_id = ${chat}
            AND user_id = ${user} AND created_at > ${now - 900}
            AND batch_id IS NULL AND (mime LIKE 'image/%' OR mime = ${BATCH})
    `);
}

async function register(message, image, batchId = null) {
    const now = clock();
    const chat = message.chat.id;
    const user = message.from.id;
    const messageId = message.message_id;
    await db.run(sql`DELETE FROM conversion_uploads WHERE created_at < ${now - 7200}`);
    return db.run(sql`
        INSERT INTO conversion_uploads
            (chat_id, user_id, message_id, file_id, filename, mime, short_edge, created_at, batch_id)
        SELECT ${chat}, ${user}, ${messageId}, ${image.file_id}, ${image.filename}, ${image.mime},
            0, ${now}, ${batchId}
        WHERE NOT EXISTS (
            SELECT 1 FROM conversion_uploads WHERE chat_id = ${chat} AND message_id = ${messageId}
        ) AND (
            SELECT count(*) FROM conversion_uploads WHERE user_id = ${user} AND created_at > ${now - 300}
        ) < 20 AND (${batchId} IS NULL OR (
            EXISTS (SELECT 1 FROM conversion_uploads WHERE id = ${batchId} AND started_at IS NULL
                AND finished_at IS NULL AND created_at > ${now - 900})
            AND (SELECT count(*) FROM conversion_uploads WHERE batch_id = ${batchId}) < 6
        ))
    `);
}

export async function imageMessage(message, command, send) {
    if (!message.from || message.sender_chat) return false;
    const user = message.from.id;
    const chat = message.chat.id;

    if (['/image', '/compress', '/convert', '/pdf'].includes(command)) {
        await send('Send a JPG, PNG, WebP, BMP or TIFF photo/file to convert, compress, resize, or make a PDF. Use /imagepdf for multiple images. Send a video for MP3 or MP4.');
        return true;
    }
    if (command === '/document' || command === '/unlock') {
        await send('Office conversion and unlocking PDFs are currently unavailable. Use /help for available tools.');
        return true;
    }
    if (command === '/cancel') {
        const now = clock();
        const result = await db.run(sql`
            UPDATE conversion_uploads SET finished_at = ${now}
            WHERE chat_id = ${chat} AND user_id = ${user} AND started_at IS NULL AND finished_at IS NULL
                AND batch_id IS NULL AND (mime LIKE 'image/%' OR mime = ${BATCH}) AND created_at > ${now - 900}
        `);
        await send(result.rowsAffected ? 'Pending image menus cancelled.' : 'No pending image menus.');
        return true;
    }
    if (command === '/imagepdf') {
        const result = await register(message, { file_id: '', filename: '', mime: BATCH });
        if (!result.rowsAffected) {
            await send('Try again in a few minutes.');
            return true;
        }
        const id = result.lastInsertRowid;
        await send(`Image PDF #${id}\nReply to this message with each image, in order (up to six). Wait for each confirmation, then choose a page size below.`,
            { reply_markup: keyboard(id, [['fit', 'Fit image'], ['a4', 'A4'], ['letter', 'Letter'], ['custom', 'Custom']], 'pdf') });
        return true;
    }

    const image = uploadedImage(message);
    const reply = message.reply_to_message;
    const batch = /^Image PDF #(\d+)\n/.exec(reply?.text || '');
    const custom = /^Image settings #(\d+) (jpg|webp|compress|resize|pdf)\n/.exec(reply?.text || '');
    if ((batch && image) || (custom && message.text)) {
        const me = await api.getMe();
        if (reply.from?.id === me.id) {
            if (custom && message.text) {
                const upload = await lookup(Number(custom[1]), chat, user);
                if (!upload || upload.started_at !== null || upload.finished_at !== null) {
                    await send('This menu expired or belongs to another user.');
                    return true;
                }
                const job = customSettings(custom[2], message.text);
                if (!job) {
                    await send('Those settings are invalid. Reply to the settings prompt again using its example.');
                    return true;
                }
                await runImage(upload, job, send, send);
                return true;
            }
            const id = Number(batch[1]);
            const upload = await lookup(id, chat, user);
            if (upload?.mime !== BATCH || upload.started_at !== null || upload.finished_at !== null) {
                await send('This batch expired, was cancelled, or belongs to another user.');
            } else if (image.file_size > MAX_SIZE) {
                await send('The combined image limit is 20 MB.');
            } else {
                const result = await register(message, image, id);
                await send(result.rowsAffected ? 'Image added.' : 'Image could not be added. Check the six-image limit and batch status.');
            }
            return true;
        }
    }

    if (!image) return false;
    if (image.file_size > MAX_SIZE) {
        await send('Maximum image size is 20 MB.');
        return true;
    }
    if (!(image.mime.startsWith('image/') || image.mime === 'application/octet-stream')) {
        await send('Send a supported image file.');
        return true;
    }
    // The extension determines this menu; the backend validates the actual format.
    image.mime = image.mime.startsWith('image/') ? image.mime : 'image/unknown';
    const result = await register(message, image);
    if (result.rowsAffected) {
        await send('Image received. Choose an action.', {
            reply_markup: keyboard(result.lastInsertRowid, Object.entries(ACTIONS)),
        });
    } else {
        await send('This upload already has a menu, or you have sent too many files.');
    }
    return true;
}

const CUSTOM = {
    jpg: 'Reply with quality and optional background, e.g. 90 black (35–100, white by default).',
    webp: 'Reply with quality, e.g. 90 (35–100).',
    compress: 'Reply with format and target, e.g. webp 250 KB or jpg 1 MB. Optional width: webp 250 KB 800.',
    resize: 'Reply with e.g. width 800, height 600, percent 50, exact 640 480, or fit 1280 720. Add up to allow enlargement.',
    pdf: 'Reply with page size, orientation and margin in points, e.g. a4 portrait 24 (fit/a4/letter, auto/portrait/landscape, 0–72).',
};

export function customSettings(action, raw) {
    const text = raw.trim().toLowerCase();
    let match;
    if (action === 'jpg' || action === 'webp') {
        match = /^(\d{1,3})(?: (white|black))?$/.exec(text);
        if (match && +match[1] >= 35 && +match[1] <= 100 &&
            (action === 'jpg' || !match[2])) {
            return [action, { quality: +match[1], background: match[2] }];
        }
    } else if (action === 'compress') {
        match = /^(jpg|webp) (\d{1,5})\s*(kb|mb)(?: (\d{1,4}))?$/.exec(text);
        if (match) {
            const target = +match[2] * (match[3] === 'mb' ? 1000 : 1);
            if (target > 0 && target <= 20000 && (!match[4] || (+match[4] > 0 && +match[4] <= 6000))) {
                return [match[1], { compress: true, quality: 95, target_kb: target,
                    ...(match[4] ? { mode: 'width', width: +match[4] } : {}) }];
            }
        }
    } else if (action === 'resize') {
        match = /^(width|height|percent|exact|fit) (\d{1,4})(?: (\d{1,4}))?( up)?$/.exec(text);
        if (match && Boolean(match[3]) === ['exact', 'fit'].includes(match[1])) {
            const [, mode, first, second, upscale] = match;
            return ['png', { mode, width: +first, height: +(second || first), percent: +first,
                upscale: Boolean(upscale) }];
        }
    } else if (action === 'pdf') {
        match = /^(fit|a4|letter) (auto|portrait|landscape) (\d{1,2})$/.exec(text);
        if (match && +match[3] <= 72) {
            return ['pdf', { page: match[1], orientation: match[2], margin: +match[3] }];
        }
    }
    return null;
}

export async function imageCallback(query) {
    const match = /^i:(\d+):(png|jpg|webp|compress|resize|pdf|cancel)(?::([a-z]+))?$/.exec(query.data);
    const answer = (text) => api.answerCallbackQuery({ callback_query_id: query.id, text });
    if (!match || !query.message?.date) {
        await answer('Send an image to open a fresh menu.');
        return;
    }
    const [, rawId, action, option] = match;
    const id = Number(rawId);
    const chat_id = query.message.chat.id;
    const message_id = query.message.message_id;
    const send = (text, extra = {}) => api.sendMessage({ chat_id, text, ...extra });
    const edit = (text, reply_markup = { inline_keyboard: [] }) =>
        api.editMessageText({ chat_id, message_id, text, reply_markup });
    const upload = await lookup(id, chat_id, query.from.id);
    if (!upload || upload.started_at !== null || upload.finished_at !== null ||
        (upload.mime === BATCH && !['pdf', 'cancel'].includes(action))) {
        await answer('This menu expired, was used, or belongs to another user.');
        return;
    }
    await answer();
    if (action === 'cancel') {
        const now = clock();
        const update = await db.run(sql`UPDATE conversion_uploads SET finished_at = ${now}
            WHERE id = ${id} AND started_at IS NULL AND finished_at IS NULL`);
        if (update.rowsAffected) await edit('Cancelled.');
        return;
    }
    if (option === 'custom' && CUSTOM[action]) {
        await send(`Image settings #${id} ${action}\n${CUSTOM[action]}`, {
            reply_markup: { force_reply: true },
        });
        return;
    }
    if (action === 'png' && !option) {
        await runImage(upload, ['png', {}], edit, send);
    } else if (action === 'pdf' && ['fit', 'a4', 'letter'].includes(option)) {
        await runImage(upload, ['pdf', { page: option, margin: option === 'fit' ? 0 : 24 }], send, send);
    } else if (action === 'resize' && Object.hasOwn(PRESETS, option)) {
        const [width, height] = PRESETS[option];
        await runImage(upload, ['png', { mode: 'fit', width, height }], edit, send);
    } else if (['jpg', 'webp'].includes(action) && Object.hasOwn(QUALITY, option)) {
        await runImage(upload, [action, { quality: QUALITY[option] }], edit, send);
    } else if (action === 'compress' && /^(jpg|webp|png)(low|medium|high)$/.test(option || '')) {
        const [, format, level] = /^(jpg|webp|png)(low|medium|high)$/.exec(option);
        await runImage(upload, [format, { compress: true, quality: { low: 95, medium: 82, high: 55 }[level] }], edit, send);
    } else if (!option) {
        let entries;
        if (action === 'pdf') entries = [['fit', 'Fit image'], ['a4', 'A4'], ['letter', 'Letter']];
        if (action === 'resize') entries = Object.entries(PRESETS).map(([key, size]) => [key, `Fit ${size.join(' x ')}`]);
        if (action === 'jpg' || action === 'webp') entries = [['high', 'High quality'], ['balanced', 'Balanced'], ['max', 'Maximum compression']];
        if (action === 'compress') entries = [
            ...['jpg', 'webp'].flatMap((format) => ['low', 'medium', 'high'].map((level) => [format + level, `${format.toUpperCase()} - ${level} compression`])),
            ['pngmedium', 'PNG - lossless'],
        ];
        if (entries) await edit('Choose settings.', keyboard(id, [...entries, ['custom', 'Custom']], action));
    }
}

async function runImage(upload, [format, settings], progress, send) {
    const { id, chat_id, user_id } = upload;
    const now = clock();
    const files = upload.mime === BATCH ? await db.all(sql`
        SELECT * FROM conversion_uploads WHERE batch_id = ${id} AND chat_id = ${chat_id}
            AND user_id = ${user_id} ORDER BY message_id
    `) : [upload];
    if (!files.length) {
        await send('Reply to the batch message with at least one image first.');
        return;
    }
    const setting = await db.get(sql`SELECT value FROM converter_settings WHERE name = 'api_key'`);
    if (!setting?.value) {
        await send('Conversion service setup is incomplete.');
        return;
    }
    const claimed = await db.run(sql`
        UPDATE conversion_uploads SET started_at = ${now}
        WHERE id = ${id} AND started_at IS NULL AND finished_at IS NULL AND created_at > ${now - 900}
            AND (${upload.mime} != ${BATCH} OR (SELECT count(*) FROM conversion_uploads WHERE batch_id = ${id}) = ${files.length})
            AND (SELECT count(*) FROM conversion_uploads WHERE user_id = ${user_id}
                AND started_at > ${now - 3600}) < 3
            AND NOT EXISTS (SELECT 1 FROM conversion_uploads WHERE user_id = ${user_id}
                AND finished_at IS NULL AND started_at > ${now - 300})
    `);
    if (!claimed.rowsAffected) {
        await send('Job unavailable or image batch changed. Check the menu. The limit is one active job and three attempts per hour.');
        return;
    }

    try {
        await progress('Processing images. The backend may be waking up.');
        const form = new FormData();
        let original = 0;
        for (const file of files) {
            const bytes = await api.getFileContent(file.file_id);
            original += bytes.byteLength;
            if (original > MAX_SIZE) {
                await send('Combined image size exceeds 20 MB.');
                return;
            }
            form.append('file', new InputFile(bytes, file.filename, { type: file.mime }));
        }
        form.append('output_format', format);
        form.append('options', JSON.stringify(settings));
        const response = await fetch(BACKEND + '/convert-image', {
            method: 'POST', headers: { 'x-api-key': setting.value }, body: form,
        });
        if (!response.ok) {
            const details = [400, 413, 415, 422, 504].includes(response.status) ?
                (await response.json().catch(() => ({}))).detail : undefined;
            await send(typeof details === 'string' ? details :
                response.status === 429 ? 'The converter is busy. Try again shortly.' :
                    'Image conversion is temporarily unavailable. Send the file again to retry.');
            return;
        }
        const chunks = [];
        let size = 0;
        for await (const chunk of response.body) {
            size += chunk.byteLength;
            if (size > MAX_SIZE) {
                await send('Output exceeds 20 MB.');
                return;
            }
            chunks.push(chunk);
        }
        if (!size) throw new Error('Empty result.');
        const output = new Uint8Array(size);
        let offset = 0;
        for (const chunk of chunks) {
            output.set(chunk, offset);
            offset += chunk.byteLength;
        }
        const reduction = (100 * (original - size) / original).toFixed(1);
        let caption = `Input ${original.toLocaleString()} bytes. Output ${size.toLocaleString()} bytes.`;
        if (settings.compress) caption += size < original ?
            ` Reduced by ${reduction}%.` : size === original ? ' Size unchanged.' : ' Output is larger than input.';
        if (response.headers.get('x-target-met') === 'false') caption += ' Target size was not reached.';
        await api.sendDocument({ chat_id, caption, document: new InputFile(output, `converted.${format}`, { type: MIME[format] }) });
    } catch {
        console.error('Image conversion workflow failed.');
        await send('Image conversion or delivery failed. Send the file again to retry.');
    } finally {
        const finished = clock();
        await db.run(sql`UPDATE conversion_uploads SET finished_at = ${finished} WHERE id = ${id}`);
    }
}
