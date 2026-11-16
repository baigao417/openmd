export type ProviderMode = "auto" | "tingwu" | "get";
export type TranscriptProviderName = "tingwu" | "get";
export type MediaExtractor = "dom" | "network" | "record" | "cat-catch";

export interface BrowserCookie {
  name: string;
  value: string;
  domain: string;
  path: string;
  secure: boolean;
  httpOnly: boolean;
  expirationDate?: number;
}

export interface WorkflowOptions {
  url: string;
  outDir: string;
  provider: ProviderMode;
  keepArtifacts: boolean;
  timeoutMinutes: number;
  outputTitle?: string;
  resourceId?: string;
}

export interface MediaDiscovery {
  pageTitle: string;
  sourceUrl: string;
  mediaUrl: string;
  candidateMediaUrls?: string[];
  expectedDurationSec?: number;
  extractor: MediaExtractor;
  mimeType?: string;
  referer?: string;
  userAgent?: string;
  browserCookies?: BrowserCookie[];
}

export interface DownloadResult {
  originalPath: string;
  audioPath: string;
  mediaDurationSec: number;
  cleanupPaths: string[];
}

export interface DownloadAttemptSnapshot {
  status: "running" | "success" | "failed";
  strategy: string;
  attempt: number;
  attemptLabel: string;
  attemptPath?: string;
  path?: string;
  bytes?: number;
  sizeMB?: number;
  lastWriteTime?: string;
  startedAt?: string;
  finishedAt?: string;
  error?: string;
  segments?: {
    total: number;
    success: number;
    pending: number;
    failed: number;
  };
}

export interface DownloadBenchmarkSnapshot {
  status: "running" | "success" | "failed";
  strategy: string;
  label: string;
  sampleDurationSec?: number;
  wallMs?: number;
  bytes?: number;
  effectiveSpeed?: number;
  normalizedAudio: boolean;
  error?: string;
}

export interface DownloadBenchmarkStatusSnapshot {
  status: "running" | "success" | "failed";
  plans: DownloadBenchmarkSnapshot[];
  selected?: DownloadBenchmarkSnapshot;
}

export interface DownloadStatusSnapshot {
  status: "idle" | "running" | "success" | "failed";
  current?: DownloadAttemptSnapshot;
  attempts?: DownloadAttemptSnapshot[];
  benchmark?: DownloadBenchmarkStatusSnapshot;
}

export interface TranscriptChapter {
  title: string;
  timestamp: string;
  startSec: number;
}

export interface TranscriptSegment {
  timestamp: string;
  startSec: number;
  text: string;
  speaker?: string;
}

export interface TranscriptResult {
  provider: TranscriptProviderName;
  title: string;
  transcriptDurationSec: number;
  chapters: TranscriptChapter[];
  segments: TranscriptSegment[];
  rawText?: string;
}

export interface ProviderInput {
  audioPath: string;
  sourceUrl: string;
  resourceId?: string;
  preferredTitle: string;
  tempDir: string;
  timeoutMinutes: number;
}

export interface TranscriptProvider {
  transcribe(input: ProviderInput): Promise<TranscriptResult>;
}

export type WorkflowCoverageStatus = "complete" | "partial";

export interface WorkflowProviderAttempt {
  provider: TranscriptProviderName;
  status: "success" | "failed";
  coverage?: number;
  error?: string;
  startedAt: string;
  finishedAt: string;
}

export interface WorkflowResult {
  outputPath: string;
  provider: TranscriptProviderName;
  coverage: number;
  coverageStatus?: WorkflowCoverageStatus;
  warnings?: string[];
  providerAttempts?: WorkflowProviderAttempt[];
  tempDir: string;
}

export interface WorkflowStageEvent {
  stage: string;
  status: "started" | "completed" | "failed";
  detail?: string;
  download?: DownloadStatusSnapshot;
}

export interface WorkflowDependencies {
  assertEnvironment: () => Promise<void>;
  createTempDir: () => Promise<string>;
  discoverMedia: (url: string, tempDir: string) => Promise<MediaDiscovery>;
  downloadMedia: (
    media: MediaDiscovery,
    tempDir: string,
    timeoutMinutes?: number,
    onStatus?: (status: DownloadStatusSnapshot) => Promise<void> | void
  ) => Promise<DownloadResult>;
  providers: Record<TranscriptProviderName, TranscriptProvider>;
  writeMarkdownFile: (filePath: string, content: string) => Promise<void>;
  cleanupArtifacts: (paths: string[]) => Promise<void>;
}

export interface MarkdownInput {
  title: string;
  sourceUrl: string;
  resourceId?: string;
  provider: TranscriptProviderName;
  mediaDurationSec: number;
  coverage: number;
  generatedAt: string;
  chapters: TranscriptChapter[];
  segments: TranscriptSegment[];
  rawText?: string;
}

export interface XiaohongshuNoteSummary {
  id: string;
  title: string;
  type: string;
  likes?: string;
  url: string;
  cover?: string;
}

export interface XiaohongshuBatchOptions {
  profileUrl: string;
  creatorName: string;
  creatorHandle: string;
  outDir: string;
  limit: number;
  provider: ProviderMode;
  keepArtifacts: boolean;
}

export interface XiaohongshuBatchItemResult {
  seq: string;
  noteId: string;
  title: string;
  url: string;
  likes?: string;
  status: "success" | "failed";
  itemDir: string;
  downloadDir: string;
  metaPath: string;
  transcriptPath?: string;
  downloadStatus: "pending" | "success" | "failed" | "skipped";
  transcriptStatus: "pending" | "success" | "failed" | "skipped";
  error?: string;
  startedAt: string;
  finishedAt: string;
}

export interface XiaohongshuBatchResult {
  creatorDir: string;
  items: XiaohongshuBatchItemResult[];
}

export type GetTranscriptContentSource = "linked_web" | "media_text" | "summary" | "cleaned_summary" | "dom";

export interface GetSubscriptionSummary {
  creatorId: string;
  creatorName: string;
  subjectUrl: string;
  statsInfo?: string;
  rootDirectoryId?: number;
}

export interface GetCollectedTranscriptNote {
  noteId?: string;
  title: string;
  publishedAt?: string;
  detailUrl?: string;
  sourceUrl?: string;
  content?: string;
  contentSource?: GetTranscriptContentSource;
  error?: string;
  pageNumber?: number;
  rowIndex?: number;
}

export interface GetCollectedTranscriptResult {
  creatorId: string;
  creatorName: string;
  subjectUrl: string;
  items: GetCollectedTranscriptNote[];
}

export interface GetBatchExportOptions {
  subjectUrl?: string;
  creatorId?: string;
  creatorName?: string;
  outDir: string;
  startDate?: string;
  endDate?: string;
  skipExisting: boolean;
  includeComments?: boolean;
  commentLimit?: number;
}

export interface GetBatchExportItemResult {
  noteId?: string;
  title: string;
  publishedAt?: string;
  transcriptPath?: string;
  detailUrl?: string;
  sourceUrl?: string;
  status: "success" | "failed" | "skipped";
  contentSource?: GetTranscriptContentSource;
  awemeId?: string;
  collectionId?: string;
  collectionName?: string;
  commentStatus?: "success" | "failed" | "skipped";
  commentCount?: number;
  commentError?: string;
  error?: string;
}

export interface DouyinCommentSummary {
  text: string;
  diggCount: number;
  nickname: string;
}

export interface DouyinVideoSummary {
  awemeId: string;
  title: string;
  sourceUrl: string;
  playUrl: string;
  durationSec: number;
  diggCount: number;
  commentCount: number;
  shareCount: number;
  collectCount: number;
  createTime?: string;
  creatorName: string;
  creatorSecUid: string;
  collectionId?: string;
  collectionName?: string;
}

export interface DouyinBatchExportOptions {
  sourceUrl?: string;
  secUid?: string;
  outDir: string;
  limit?: number;
  commentLimit?: number;
  includeComments: boolean;
  skipExisting: boolean;
  keepArtifacts: boolean;
}

export interface DouyinBatchExportItemResult {
  awemeId: string;
  title: string;
  sourceUrl: string;
  collectionId?: string;
  collectionName?: string;
  videoPath?: string;
  transcriptPath?: string;
  status: "success" | "failed" | "skipped";
  downloadStatus: "pending" | "success" | "failed" | "skipped";
  transcriptStatus: "pending" | "success" | "failed" | "skipped";
  commentStatus: "pending" | "success" | "failed" | "skipped";
  commentCount?: number;
  error?: string;
}

export interface DouyinBatchExportResult {
  creatorDir: string;
  creatorName: string;
  creatorSecUid: string;
  total: number;
  success: number;
  skipped: number;
  failed: number;
  items: DouyinBatchExportItemResult[];
}

export interface GetBatchExportResult {
  creatorDir: string;
  creatorId: string;
  creatorName: string;
  total: number;
  success: number;
  skipped: number;
  failed: number;
  items: GetBatchExportItemResult[];
}

export interface GetExportConfig {
  defaultOutputDir: string;
}

export interface GetExportRunState {
  status: "idle" | "running" | "success" | "error";
  currentStage?: string;
  total?: number;
  successCount?: number;
  skippedCount?: number;
  failedCount?: number;
  log?: string[];
  lastRun?: {
    subjectUrl?: string;
    creatorId?: string;
    creatorName?: string;
    creatorDir?: string;
    total?: number;
    success?: number;
    skipped?: number;
    failed?: number;
    error?: string;
    startedAt: string;
    finishedAt?: string;
  };
}
