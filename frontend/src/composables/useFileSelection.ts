/**
 * 文件选择 composable：拖拽状态、客户端校验（扩展名/数量/大小）、chips 增删。
 * 校验规则与后端 API 一致，先行报错：
 *  - 扩展名 pdf / png / jpg / jpeg
 *  - 单次最多 10 个
 *  - 单文件 ≤ 20MB
 */

import { computed, ref } from "vue";

const ALLOWED_EXTENSIONS = ["pdf", "png", "jpg", "jpeg"] as const;
const MAX_FILES = 10;
const MAX_FILE_MB = 20;
const MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024;

/** 校验规则常量：供组件 aria-label 等展示文案复用，避免硬编码漂移。 */
export const UPLOAD_LIMITS = {
  extensions: ALLOWED_EXTENSIONS,
  maxFiles: MAX_FILES,
  maxFileMB: MAX_FILE_MB,
} as const;

export interface SelectedFile {
  file: File;
  sizeLabel: string;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function useFileSelection() {
  const files = ref<SelectedFile[]>([]);
  const errors = ref<string[]>([]);
  const isDragging = ref(false);
  let dragDepth = 0;

  /** 有文件可提交（非法文件不会进入列表，故只看数量）。 */
  const canSubmit = computed(() => files.value.length > 0);

  function hasFiles(e: DragEvent): boolean {
    const types = e.dataTransfer?.types;
    if (!types) return false;
    for (let i = 0; i < types.length; i += 1) {
      if (types[i] === "Files") return true;
    }
    return false;
  }

  function validateFile(file: File): string | null {
    const ext = (file.name.split(".").pop() ?? "").toLowerCase();
    if (!(ALLOWED_EXTENSIONS as readonly string[]).includes(ext)) {
      return `不支持的文件类型：${file.name}（仅支持 PDF / PNG / JPG / JPEG）`;
    }
    if (file.size > MAX_FILE_BYTES) {
      return `文件过大：${file.name}（单文件最大 ${MAX_FILE_MB} MB）`;
    }
    return null;
  }

  /** 把新文件并入列表：逐项校验，并把错误写入 errors。 */
  function addValidated(list: FileList | File[]) {
    const incoming = Array.from(list);
    const seen = new Set(
      files.value.map((f) => `${f.file.name}\u0000${f.file.size}`),
    );
    const accepted: File[] = [];
    const problems: string[] = [];

    for (const file of incoming) {
      if (files.value.length + accepted.length >= MAX_FILES) break;
      const problem = validateFile(file);
      if (problem !== null) {
        problems.push(problem);
        continue;
      }
      const key = `${file.name}\u0000${file.size}`;
      if (seen.has(key)) {
        problems.push(`文件重复：${file.name}`);
        continue;
      }
      seen.add(key);
      accepted.push(file);
    }

    const truncated = incoming.length - accepted.length - problems.length;
    if (truncated > 0) {
      problems.push(
        `文件数量超过限制（单次最多 ${MAX_FILES} 个，已忽略 ${truncated} 个）`,
      );
    }
    if (problems.length > 0) errors.value.push(...problems);
    if (accepted.length > 0) {
      files.value.push(
        ...accepted.map((f) => ({ file: f, sizeLabel: formatSize(f.size) })),
      );
    }
  }

  /** 用户主动添加（拖放/选择器）：先清空旧错误，再校验并入。 */
  function accept(list: FileList | File[]) {
    errors.value = [];
    addValidated(list);
  }

  function removeFile(index: number) {
    files.value.splice(index, 1);
  }

  function clearErrors() {
    errors.value = [];
  }

  function onDragEnter(e: DragEvent) {
    if (!hasFiles(e)) return;
    dragDepth += 1;
    isDragging.value = true;
  }

  function onDragOver(e: DragEvent) {
    e.preventDefault(); // 必须 preventDefault 才能触发 drop
  }

  function onDragLeave() {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) isDragging.value = false;
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragDepth = 0;
    isDragging.value = false;
    const dropped = e.dataTransfer?.files;
    if (dropped && dropped.length > 0) accept(dropped);
  }

  return {
    files,
    errors,
    isDragging,
    canSubmit,
    accept,
    removeFile,
    clearErrors,
    onDragEnter,
    onDragOver,
    onDragLeave,
    onDrop,
  };
}
