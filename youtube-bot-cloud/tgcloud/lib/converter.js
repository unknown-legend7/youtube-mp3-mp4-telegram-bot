export const BACKEND = 'https://media-converter-api-zoio.onrender.com';
export const MAX_SIZE = 20_000_000;
export const AUDIO = ['64k', '128k', '192k', '256k', '320k'];
export const VIDEO = ['360p', '480p', '720p', '1080p'];

export function videoChoices(shortEdge) {
    const choices = VIDEO.filter((quality) => Number.parseInt(quality, 10) <= shortEdge);
    return choices.length ? choices : ['source'];
}
