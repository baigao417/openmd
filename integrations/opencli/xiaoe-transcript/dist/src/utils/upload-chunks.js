export function splitBufferToBase64Chunks(buffer, chunkSizeBytes = 512 * 1024) {
    const chunks = [];
    for (let offset = 0; offset < buffer.length; offset += chunkSizeBytes) {
        chunks.push(buffer.subarray(offset, Math.min(buffer.length, offset + chunkSizeBytes)).toString('base64'));
    }
    return chunks;
}
export function joinBase64Chunks(chunks) {
    return Buffer.concat(chunks.map((chunk) => Buffer.from(chunk, "base64")));
}
