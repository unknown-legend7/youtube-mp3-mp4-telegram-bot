import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { DatabaseSync } from 'node:sqlite';

const sql = (parts, ...values) => [parts.join('?'), values.map((value) => value ?? null)];

function setup() {
    const database = new DatabaseSync(':memory:');
    database.exec(`
        CREATE TABLE conversion_uploads (
            id INTEGER PRIMARY KEY, chat_id INTEGER, user_id INTEGER, message_id INTEGER,
            file_id TEXT, filename TEXT, mime TEXT, short_edge INTEGER, created_at INTEGER,
            started_at INTEGER, finished_at INTEGER, batch_id INTEGER);
        CREATE TABLE converter_settings (name TEXT, value TEXT);
        INSERT INTO converter_settings VALUES ('api_key', 'test-key');
    `);
    const records = { messages: [], answers: [], documents: [], requests: [] };
    const db = {
        run: async ([query, values]) => {
            const result = database.prepare(query).run(...values);
            return { rowsAffected: result.changes, lastInsertRowid: result.lastInsertRowid };
        },
        all: async ([query, values]) => database.prepare(query).all(...values),
        get: async ([query, values]) => database.prepare(query).get(...values),
    };
    const api = {
        getMe: async () => ({ id: 99 }),
        getFileContent: async (id) => new TextEncoder().encode(id),
        sendMessage: async (message) => { records.messages.push(message); },
        editMessageText: async (message) => { records.messages.push(message); },
        answerCallbackQuery: async (answer) => { records.answers.push(answer); },
        sendDocument: async (document) => { records.documents.push(document); },
    };
    class InputFile {
        constructor(bytes, filename, options) { Object.assign(this, { bytes, filename, ...options }); }
    }
    class FormData extends Array {
        append(key, value) { this.push([key, value]); }
    }
    const fetch = async (url, request) => {
        records.requests.push({ url, request });
        return {
            ok: true, headers: { get: () => 'false' },
            body: [new Uint8Array([1, 2, 3])],
        };
    };
    globalThis.testSdk = { api, db, fetch, FormData, InputFile, sql };
    return { database, records };
}

async function handler(name) {
    // Isolate the SDK bindings per test, including imports from shared modules.
    const { SourceTextModule, SyntheticModule, createContext } = await import('node:vm');
    const context = createContext({ ...globalThis, Uint8Array, TextEncoder, console });
    const cache = new Map();
    async function moduleFor(url) {
        if (cache.has(url)) return cache.get(url);
        const module = url === 'sdk' || url === 'sdk/db'
            ? new SyntheticModule(Object.keys(globalThis.testSdk), function () {
                for (const [key, value] of Object.entries(globalThis.testSdk)) this.setExport(key, value);
            }, { context, identifier: url })
            : new SourceTextModule(await readFile(new URL(url), 'utf8'), { context, identifier: url });
        cache.set(url, module);
        return module;
    }
    const module = await moduleFor(new URL('./tgcloud/handlers/' + name + '.js', import.meta.url).href);
    await module.link((specifier, parent) => moduleFor(
        specifier.startsWith('sdk') ? specifier : new URL(specifier, parent.identifier).href));
    await module.evaluate();
    return module.namespace.default;
}

const message = (message_id, extra, user = 10) => ({ message_id, date: 1, chat: { id: 7 }, from: { id: user }, ...extra });
const query = (data, user = 10) => ({ id: 'callback', data, from: { id: user }, message: message(100, {}) });

test('photo menu, owner protection, one delivery and unchanged media button routing', async () => {
    const { database, records } = setup();
    try {
        const onMessage = await handler('message');
        const onCallback = await handler('callback_query');
        await onMessage(message(1, { photo: [{ file_id: 'small' }, { file_id: 'large' }] }));
        const png = records.messages.at(-1).reply_markup.inline_keyboard[0][0].callback_data;
        await onCallback(query(png, 11));
        assert.match(records.answers.at(-1).text, /another user/);
        assert.equal(records.requests.length, 0);
        await onCallback(query(png));
        await onCallback(query(png));
        assert.equal(records.documents.length, 1);
        assert.equal(records.documents[0].document.filename, 'converted.png');
        assert.deepEqual(Array.from(records.requests[0].request.body[0][1].bytes), Array.from(new TextEncoder().encode('large')));
        await onMessage(message(2, { video: { file_id: 'video', mime_type: 'video/mp4', width: 1280, height: 720 } }));
        const mp3 = records.messages.at(-1).reply_markup.inline_keyboard[0][0].callback_data;
        assert.match(mp3, /^m:\d+:mp3$/);
        await onCallback(query(mp3));
        assert.match(records.messages.at(-1).reply_markup.inline_keyboard[0][0].callback_data, /^q:\d+:mp3:64k$/);
    } finally {
        database.close();
    }
});

test('batch files follow message order, custom settings apply and pending menus cancel', async () => {
    const { database, records } = setup();
    try {
        const onMessage = await handler('message');
        const onCallback = await handler('callback_query');
        await onMessage(message(1, { text: '/imagepdf' }));
        const { text, reply_markup } = records.messages.at(-1);
        const reply_to_message = { text, from: { id: 99 } };
        await onMessage(message(3, { photo: [{ file_id: 'second' }], reply_to_message }));
        await onMessage(message(2, { photo: [{ file_id: 'first' }], reply_to_message }));
        await onCallback(query(reply_markup.inline_keyboard.at(-1)[0].callback_data));
        const prompt = records.messages.at(-1).text;
        await onMessage(message(4, { text: 'letter landscape 15', reply_to_message: { text: prompt, from: { id: 99 } } }));
        assert.equal(records.documents[0].document.filename, 'converted.pdf');
        const { body } = records.requests[0].request;
        assert.equal(new TextDecoder().decode(body[0][1].bytes), 'first');
        assert.equal(new TextDecoder().decode(body[1][1].bytes), 'second');
        assert.deepEqual(JSON.parse(body.find(([name]) => name === 'options')[1]),
            { page: 'letter', orientation: 'landscape', margin: 15 });
        await onMessage(message(5, { photo: [{ file_id: 'unused' }] }));
        const png = records.messages.at(-1).reply_markup.inline_keyboard[0][0].callback_data;
        await onMessage(message(6, { text: '/cancel' }));
        await onCallback(query(png));
        assert.equal(records.requests.length, 1);
    } finally {
        database.close();
    }
});
