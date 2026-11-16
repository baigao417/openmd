export interface GetStreamEvent {
  msgType: number;
  payload: Record<string, unknown>;
}

export interface GetStreamSummary {
  noteId?: string;
  sections: Array<Record<string, unknown>>;
  sentences: Array<Record<string, unknown>>;
  summaryText: string;
  done: boolean;
}

function parseJson(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
}

export function parseGetEventStream(input: string): GetStreamEvent[] {
  const chunks = input.split(/\n\n+/).map((chunk) => chunk.trim()).filter(Boolean);
  const events: GetStreamEvent[] = [];

  for (const chunk of chunks) {
    const dataLines = chunk
      .split(/\n/)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim())
      .filter(Boolean);

    for (const line of dataLines) {
      const payload = parseJson(line);
      if (!payload || typeof payload !== 'object') {
        continue;
      }
      const msgType = Number((payload as { msg_type?: unknown }).msg_type);
      if (Number.isNaN(msgType)) {
        continue;
      }
      events.push({ msgType, payload: payload as Record<string, unknown> });
    }
  }

  return events;
}

function parseEmbeddedJson(payload: Record<string, unknown>): Record<string, unknown> | undefined {
  const data = payload.data;
  if (!data || typeof data !== 'object') {
    return undefined;
  }
  const msg = (data as { msg?: unknown }).msg;
  if (typeof msg !== 'string') {
    return undefined;
  }
  const parsed = parseJson(msg);
  return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : undefined;
}

export function summarizeGetStream(events: GetStreamEvent[]): GetStreamSummary {
  const summary: GetStreamSummary = {
    sections: [],
    sentences: [],
    summaryText: '',
    done: false
  };

  for (const event of events) {
    if (event.msgType === -1) {
      const data = event.payload.data;
      if (data && typeof data === 'object' && typeof (data as { note_id?: unknown }).note_id === 'string') {
        summary.noteId = (data as { note_id: string }).note_id;
      }
      continue;
    }

    if (event.msgType === -6) {
      const parsed = parseEmbeddedJson(event.payload);
      const sections = parsed?.section_list;
      if (Array.isArray(sections)) {
        summary.sections = sections.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object');
      }
      continue;
    }

    if (event.msgType === -7) {
      const parsed = parseEmbeddedJson(event.payload);
      const sentences = parsed?.sentence_list;
      if (Array.isArray(sentences)) {
        summary.sentences = sentences.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object');
      }
      continue;
    }

    if (event.msgType === 1) {
      const parsed = parseEmbeddedJson(event.payload);
      const content = parsed?.content;
      if (typeof content === 'string') {
        summary.summaryText += content;
      }
      continue;
    }

    if (event.msgType === 101) {
      const data = event.payload.data;
      if (data && typeof data === 'object' && (data as { msg?: unknown }).msg === 'stop') {
        summary.done = true;
      }
    }
  }

  return summary;
}
