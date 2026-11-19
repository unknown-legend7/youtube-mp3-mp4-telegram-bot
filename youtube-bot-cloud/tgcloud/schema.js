import { index, integer, table, text } from 'sdk/db';

export const uploads = table('conversion_uploads', {
    id: integer('id').primaryKey({ autoIncrement: true }),
    chatId: integer('chat_id').notNull(),
    userId: integer('user_id').notNull(),
    messageId: integer('message_id').notNull(),
    fileId: text('file_id').notNull(),
    filename: text('filename').notNull(),
    mime: text('mime').notNull(),
    shortEdge: integer('short_edge').notNull(),
    createdAt: integer('created_at').notNull(),
    startedAt: integer('started_at'),
    finishedAt: integer('finished_at'),
}, (t) => ({
    byUser: index('conversion_uploads_user').on(t.userId, t.startedAt),
    byAge: index('conversion_uploads_age').on(t.createdAt),
}));

export const settings = table('converter_settings', {
    name: text('name').primaryKey(),
    value: text('value').notNull(),
});
