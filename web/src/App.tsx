import { useCallback, useEffect, useState } from 'react';
import {
  BatchState,
  RejectReason,
  RequestTimeoutError,
  SubmitResponse,
  createBatch,
  fetchBatch,
  submitBarcode,
} from './api';

const BATCH_ID_KEY = 'lab-handover-batch-id';

const REJECT_TEXT: Record<RejectReason, string> = {
  stale_conflict: '迟到请求：该序号此前已确认且条码不一致，本次内容已忽略',
  out_of_order: '跳号请求：服务端只接受当前期望序号，本次未计数',
  unknown_barcode: '条码不属于创建时冻结的批次，本次未计数',
  barcode_already_accepted: '该条码此前已接收，不重复计数',
  completed: '批次已完成，不再接受新扫描',
};

export default function App() {
  const [batch, setBatch] = useState<BatchState | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<{ kind: 'info' | 'error'; text: string } | null>(
    null,
  );

  // 创建批次表单
  const [batchInput, setBatchInput] = useState('');
  const [creating, setCreating] = useState(false);

  // 扫码提交表单；seq 默认跟随服务端 expected_seq，超时后保持原值以便重试
  const [seqInput, setSeqInput] = useState('1');
  const [barcodeInput, setBarcodeInput] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const applyState = useCallback((next: BatchState) => {
    setBatch(next);
    setSeqInput(String(next.expected_seq));
  }, []);

  const loadBatch = useCallback(
    async (id: string) => {
      try {
        applyState(await fetchBatch(id));
      } catch (err) {
        setNotice({ kind: 'error', text: `加载批次失败：${(err as Error).message}` });
      }
    },
    [applyState],
  );

  // 刷新页面后仍停在同一准确进度
  useEffect(() => {
    const stored = window.localStorage.getItem(BATCH_ID_KEY);
    if (stored) {
      loadBatch(stored).finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, [loadBatch]);

  const onCreate = async () => {
    const codes = batchInput
      .split(/[\n,，;；\s]+/)
      .map((code) => code.trim())
      .filter(Boolean);
    if (codes.length < 2 || codes.length > 20) {
      setNotice({ kind: 'error', text: '请输入 2 至 20 个唯一样本条码' });
      return;
    }
    if (new Set(codes).size !== codes.length) {
      setNotice({ kind: 'error', text: '条码必须全部唯一' });
      return;
    }
    setCreating(true);
    setNotice(null);
    try {
      const created = await createBatch(codes);
      window.localStorage.setItem(BATCH_ID_KEY, created.id);
      applyState(created);
      setBatchInput('');
      setNotice({
        kind: 'info',
        text: `批次已创建并冻结，共 ${created.total} 个样本，请从序号 1 开始扫码。`,
      });
    } catch (err) {
      setNotice({ kind: 'error', text: `创建失败：${(err as Error).message}` });
    } finally {
      setCreating(false);
    }
  };

  const onSubmit = async () => {
    if (!batch) return;
    const seq = Number.parseInt(seqInput, 10);
    if (!Number.isInteger(seq) || seq < 1) {
      setNotice({ kind: 'error', text: '序号必须是从 1 开始的正整数' });
      return;
    }
    if (!barcodeInput.trim()) {
      setNotice({ kind: 'error', text: '请输入或扫描条码' });
      return;
    }
    setSubmitting(true);
    setNotice(null);
    try {
      const res: SubmitResponse = await submitBarcode(batch.id, seq, barcodeInput.trim());
      applyState({
        id: batch.id,
        state: res.state,
        expected_seq: res.expected_seq,
        total: res.total,
        barcodes: batch.barcodes,
        confirmed: res.confirmed,
      });

      if (res.accepted && res.replay) {
        setNotice({
          kind: 'info',
          text: `序号 ${seq} 为重试，返回 ${res.confirmed_at?.slice(11, 19)} 的原确认，未重复计数。下一次请使用序号 ${res.expected_seq}。`,
        });
      } else if (res.accepted) {
        setBarcodeInput('');
        setNotice({
          kind: 'info',
          text:
            res.state === 'complete'
              ? `已确认序号 ${seq}，全部样本接收完毕，批次完成。`
              : `已确认序号 ${seq}，下一次请使用序号 ${res.expected_seq}。`,
        });
      } else {
        // 迟到 / 跳号 / 条码不属于批次 / 已完成：显示权威期望序号
        setNotice({
          kind: 'error',
          text: `${REJECT_TEXT[res.reason as RejectReason]}。当前应使用序号 ${res.expected_seq}。`,
        });
      }
    } catch (err) {
      if (err instanceof RequestTimeoutError) {
        // 关键：保留原 seq 与条码，操作员直接重试即可
        setNotice({
          kind: 'error',
          text: `请求超时，服务端可能已确认该条码。请保持序号 ${seq} 与条码不变，直接重新提交。`,
        });
      } else {
        setNotice({ kind: 'error', text: (err as Error).message });
        // 网络层异常后与服务端重新对齐，保证界面进度准确
        await loadBatch(batch.id);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const onNewBatch = () => {
    window.localStorage.removeItem(BATCH_ID_KEY);
    setBatch(null);
    setSeqInput('1');
    setBarcodeInput('');
    setNotice(null);
  };

  if (loading) {
    return <main className="page"><p>正在恢复交接进度…</p></main>;
  }

  return (
    <main className="page">
      <h1>实验室样本交接窗口</h1>

      {!batch ? (
        <section className="card">
          <h2>创建交接批次</h2>
          <p className="hint">
            输入 2–20 个唯一样本条码（换行、空格或逗号分隔）。创建后条码集合在服务端冻结。
          </p>
          <textarea
            rows={8}
            value={batchInput}
            placeholder={'S001\nS002\nS003'}
            onChange={(e) => setBatchInput(e.target.value)}
          />
          <button type="button" onClick={() => void onCreate()} disabled={creating}>
            {creating ? '创建中…' : '创建批次'}
          </button>
          {notice && <p className={notice.kind === 'error' ? 'error' : 'ok'}>{notice.text}</p>}
        </section>
      ) : (
        <section className="card">
          <div className="batch-head">
            <h2>批次 {batch.id.slice(0, 8)}</h2>
            <span className={`badge ${batch.state}`}>
              {batch.state === 'complete' ? '已完成' : '进行中'}
            </span>
          </div>

          <div className="progress">
            <div
              className="progress-bar"
              style={{ width: `${(batch.confirmed.length / batch.total) * 100}%` }}
            />
          </div>
          <p className="hint">
            已确认 {batch.confirmed.length} / {batch.total} 个 ·
            {batch.state === 'open' ? (
              <> 下一次应使用序号 <strong>{batch.expected_seq}</strong></>
            ) : (
              <> 批次已结束，不再接受新扫描</>
            )}
          </p>

          <div className="scan-row">
            <label>
              客户端序号
              <input
                type="number"
                min={1}
                value={seqInput}
                onChange={(e) => setSeqInput(e.target.value)}
                disabled={submitting || batch.state === 'complete'}
              />
            </label>
            <label className="grow">
              样本条码
              <input
                type="text"
                value={barcodeInput}
                placeholder="扫描或输入条码"
                onChange={(e) => setBarcodeInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void onSubmit();
                }}
                disabled={submitting || batch.state === 'complete'}
                autoFocus
              />
            </label>
            <button
              type="button"
              onClick={() => void onSubmit()}
              disabled={submitting || batch.state === 'complete'}
            >
              {submitting ? '提交中…' : '提交 / 超时重试'}
            </button>
          </div>

          {notice && <p className={notice.kind === 'error' ? 'error' : 'ok'}>{notice.text}</p>}

          <h3>批次条码（冻结顺序）</h3>
          <table>
            <thead>
              <tr>
                <th>序号</th>
                <th>条码</th>
                <th>状态</th>
                <th>确认时间</th>
              </tr>
            </thead>
            <tbody>
              {batch.barcodes.map((code, index) => {
                const confirmed = batch.confirmed.find((c) => c.barcode === code);
                return (
                  <tr key={code} className={confirmed ? 'done' : ''}>
                    <td>{index + 1}</td>
                    <td>{code}</td>
                    <td>{confirmed ? `已确认（序号 ${confirmed.seq}）` : '待接收'}</td>
                    <td>{confirmed ? confirmed.confirmed_at.slice(11, 19) : '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <div className="actions">
            <button type="button" className="secondary" onClick={() => batch && void loadBatch(batch.id)}>
              与服务端同步进度（刷新）
            </button>
            <button type="button" className="secondary" onClick={onNewBatch}>
              新建另一个批次
            </button>
          </div>
        </section>
      )}
    </main>
  );
}
