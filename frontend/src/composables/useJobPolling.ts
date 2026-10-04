/**
 * 任务轮询 composable：每 1s 轮询 GET /api/v1/tenants/{tenantId}/jobs/{id}，上限 60 次。
 * 成功 → 拉取 /result 后定格；失败/超时/连接错误 → 给出提示（status 区分）。
 */

import { ref } from "vue";
import {
  fetchResult,
  getJob,
  type JobDto,
  type JobProgress,
  type ResultDto,
} from "../api/client";

const POLL_INTERVAL_MS = 1000;
const MAX_ATTEMPTS = 60;

export type PollStatus =
  | "idle"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "timeout"
  | "connection-error";

export interface PollState {
  status: PollStatus;
  job: JobDto | null;
  result: ResultDto | null;
  progress: JobProgress | null;
  error: string;
}

export function useJobPolling() {
  const state = ref<PollState>({
    status: "idle",
    job: null,
    result: null,
    progress: null,
    error: "",
  });

  let timer: ReturnType<typeof setInterval> | null = null;
  let attempts = 0;

  function stop() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  /** 单次轮询；返回 false 表示轮询应停止。 */
  async function pollOnce(jobId: string): Promise<boolean> {
    let job: JobDto;
    try {
      job = await getJob(jobId);
    } catch (err) {
      stop();
      state.value = {
        ...state.value,
        status: "connection-error",
        error: err instanceof Error ? err.message : "请求失败，请检查后端服务",
      };
      return false;
    }

    if (job.status === "succeeded" || job.status === "partial") {
      stop();
      let result: ResultDto | null = null;
      try {
        result = await fetchResult(jobId);
      } catch {
        /* 结果拉取失败不阻断成功态，预览用 job.result 兜底 */
      }
      state.value = {
        ...state.value,
        status: job.status === "partial" ? "partial" : "succeeded",
        job,
        result,
        progress: job.progress,
      };
      return false;
    }

    if (job.status === "failed") {
      stop();
      state.value = {
        ...state.value,
        status: "failed",
        job,
        progress: job.progress,
        error: job.error ?? "任务处理失败",
      };
      return false;
    }

    state.value = { ...state.value, job, progress: job.progress };
    return true;
  }

  function start(jobId: string) {
    stop();
    attempts = 0;
    state.value = {
      status: "running",
      job: null,
      result: null,
      progress: null,
      error: "",
    };

    void (async () => {
      const keepGoing = await pollOnce(jobId);
      if (!keepGoing) return;
      timer = setInterval(() => {
        attempts += 1;
        if (attempts >= MAX_ATTEMPTS) {
          stop();
          state.value = {
            ...state.value,
            status: "timeout",
            error: `处理超时：超过约 ${MAX_ATTEMPTS} 秒未完成，请稍后重试`,
          };
          return;
        }
        void pollOnce(jobId);
      }, POLL_INTERVAL_MS);
    })();
  }

  return { state, start, stop };
}
