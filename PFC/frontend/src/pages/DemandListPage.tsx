import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Upload, X } from "lucide-react";
import { api } from "../api";
import type { Demand, JdRequirements } from "../api/types";
import {
  BandChip,
  Banner,
  Chip,
  EmptyState,
  ErrorState,
  Modal,
  Spinner,
} from "../components/ui";
import { UploadDialog } from "../components/UploadDialog";
import { formatDate, roleLocation } from "../lib/format";

function SkillChips({
  label,
  skills,
  onChange,
}: {
  label: string;
  skills: string[];
  onChange: (s: string[]) => void;
}) {
  const [draft, setDraft] = useState("");
  return (
    <div className="stack" style={{ gap: 4 }}>
      <strong className="small">{label}</strong>
      <div className="row">
        {skills.map((s) => (
          <Chip key={s} tone="info">
            {s}
            <button
              aria-label={`Remove ${s}`}
              onClick={() => onChange(skills.filter((x) => x !== s))}
            >
              <X size={12} />
            </button>
          </Chip>
        ))}
        <input
          aria-label={`Add ${label.toLowerCase()}`}
          placeholder="+ Add"
          value={draft}
          size={10}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && draft.trim()) {
              e.preventDefault();
              if (!skills.includes(draft.trim()))
                onChange([...skills, draft.trim()]);
              setDraft("");
            }
          }}
        />
      </div>
    </div>
  );
}

/** JD review drawer: Analyze JD → review requirements → Confirm & Start (REQ-MAT-02, §9.3). */
function JdReview({
  demand,
  onClose,
}: {
  demand: Demand;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const req = useQuery({
    queryKey: ["requirements", demand.demandId],
    queryFn: () => api.getRequirements(demand.demandId),
  });
  const [edit, setEdit] = useState<JdRequirements | null>(null);
  const value = edit ?? req.data;
  const done = () => {
    void qc.invalidateQueries();
    onClose();
  };
  const save = useMutation({
    mutationFn: (r: JdRequirements) => api.saveRequirements(r),
    onSuccess: done,
  });
  const confirm = useMutation({
    mutationFn: async (r: JdRequirements) => {
      await api.saveRequirements(r);
      await api.confirmRequirements(r.demandId);
    },
    onSuccess: done,
  });
  return (
    <Modal
      title={`JD review — ${demand.codeRequisition}`}
      onClose={onClose}
      actions={
        value && (
          <>
            <span className="small muted spacer">
              Scoring template is read-only for Analysts.
            </span>
            <button className="btn" onClick={() => save.mutate(value)}>
              Save draft
            </button>
            <button
              className="btn btn-primary"
              disabled={value.mandatorySkills.length === 0}
              onClick={() => confirm.mutate(value)}
            >
              Confirm &amp; Start ▶
            </button>
          </>
        )
      }
    >
      <span className="muted small">
        {demand.demandRequestName} · {demand.accountName} ·{" "}
        {demand.codeLocalGrade} · {roleLocation(demand.region)} · Start{" "}
        {formatDate(demand.positionStartDate)}
      </span>
      {!value ? (
        <Spinner />
      ) : (
        <div className="grid-12">
          <div className="span-6">
            <strong className="small">
              Job description (normalised, read-only)
            </strong>
            <pre
              className="card small"
              style={{ whiteSpace: "pre-wrap", fontFamily: "inherit" }}
            >
              {demand.jdText}
            </pre>
          </div>
          <div className="span-6 stack">
            {value.extractionMethod === "rule_based_fallback" && (
              <Banner tone="warning">
                <strong>Extracted by rules, not the local AI model</strong>{" "}
                (reason: {value.fallbackReason ?? "reason_not_recorded"}). Check
                every requirement carefully before Confirm &amp; Start.
              </Banner>
            )}
            {value.extractionMethod === "llm" && (
              <span className="small muted">Extracted by local AI model</span>
            )}
            <SkillChips
              label="Mandatory skills"
              skills={value.mandatorySkills}
              onChange={(s) => setEdit({ ...value, mandatorySkills: s })}
            />
            <SkillChips
              label="Preferred skills"
              skills={value.preferredSkills}
              onChange={(s) => setEdit({ ...value, preferredSkills: s })}
            />
            <label className="field">
              Experience (years)
              <input
                type="number"
                min={0}
                value={value.experienceYears ?? ""}
                onChange={(e) =>
                  setEdit({
                    ...value,
                    experienceYears:
                      e.target.value === "" ? null : Number(e.target.value),
                  })
                }
              />
            </label>
            <label className="field">
              Domain
              <input
                value={value.domain}
                onChange={(e) => setEdit({ ...value, domain: e.target.value })}
              />
            </label>
            <label className="row small">
              <input
                type="checkbox"
                checked={value.mandatoryOnsite}
                onChange={(e) =>
                  setEdit({ ...value, mandatoryOnsite: e.target.checked })
                }
              />{" "}
              Mandatory onsite
            </label>
            {value.synonymsApplied.length > 0 && (
              <span className="small muted">
                Synonyms applied: {value.synonymsApplied.join(", ")}
              </span>
            )}
            {/* The backend repeats the fallback as a warning; the banner above already shows it. */}
            {value.warnings
              .filter(
                (w) =>
                  !(
                    value.extractionMethod === "rule_based_fallback" &&
                    w.startsWith("Rule-based fallback:")
                  ),
              )
              .map((w) => (
                <Banner key={w} tone="warning">
                  {w}
                </Banner>
              ))}
          </div>
        </div>
      )}
      {(save.isError || confirm.isError) && (
        <ErrorState error={save.error ?? confirm.error} />
      )}
    </Modal>
  );
}

/** Demand List (REQ-ING-01..03, REQ-MAT-02, GUI_OCC_DESIGN.md §9). */
export function DemandListPage() {
  const qc = useQueryClient();
  const demands = useQuery({
    queryKey: ["demands"],
    queryFn: () => api.listDemands(),
  });
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState<Demand | null>(null);
  const [upload, setUpload] = useState(false);
  const analyze = useMutation({
    mutationFn: (ids: number[]) => api.analyzeDemands(ids),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["demands"] }),
  });
  const rows = useMemo(
    () =>
      (demands.data ?? []).filter((d) =>
        [
          d.codeRequisition,
          d.accountName,
          d.demandRequestName,
          d.bu,
          d.region,
        ].some((v) => (v ?? "").toLowerCase().includes(search.toLowerCase())),
      ),
    [demands.data, search],
  );
  const newOnes = (demands.data ?? []).filter(
    (d) => d.jdStatus === "Not analysed",
  );
  if (demands.isLoading)
    return (
      <div className="page">
        <Spinner />
      </div>
    );
  if (demands.isError)
    return (
      <div className="page">
        <ErrorState
          error={demands.error}
          onRetry={() => void demands.refetch()}
        />
      </div>
    );
  return (
    <div className="page">
      <div className="page-header">
        <h1>Demand List</h1>
        <span className="meta">
          Last upload: DL_Nov.xlsx · 04 Nov 2026 09:12
        </span>
        <span className="spacer" />
        <Chip>Auto-sync: Not configured</Chip>
        <button
          className="btn"
          disabled={newOnes.length === 0}
          onClick={() => analyze.mutate(newOnes.map((d) => d.demandId))}
        >
          Analyze all new ({newOnes.length})
        </button>
        <button className="btn btn-primary" onClick={() => setUpload(true)}>
          <Upload size={16} /> Upload DL
        </button>
      </div>
      <div className="card">
        <input
          aria-label="Search demands"
          placeholder="Search…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      {rows.length === 0 ? (
        <EmptyState message="No demands yet. Upload the Demand List (.xlsx)." />
      ) : (
        <div className="table-wrap">
          <table className="data" aria-label="Demands">
            <thead>
              <tr>
                <th>Code Requisition</th>
                <th>Position Start Date</th>
                <th>Account Name</th>
                <th>Code Local Grade</th>
                <th>Demand Request Name</th>
                <th>Region</th>
                <th>BU</th>
                <th>Owner</th>
                <th>JD status</th>
                <th>Matches</th>
                <th>Best band</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((d) => (
                <tr key={d.demandId}>
                  <td className="mono">
                    {d.codeRequisition}{" "}
                    {!d.idFormatOk && (
                      <span title="Not in T1/T2XXXX format (warning)">⚠</span>
                    )}
                  </td>
                  <td>{formatDate(d.positionStartDate)}</td>
                  <td>{d.accountName}</td>
                  <td>{d.codeLocalGrade}</td>
                  <td>{d.demandRequestName}</td>
                  <td>{roleLocation(d.region)}</td>
                  <td>{d.bu}</td>
                  <td>{d.owner}</td>
                  <td>
                    <Chip
                      tone={
                        d.jdStatus === "Scored"
                          ? "success"
                          : d.jdStatus === "Not analysed"
                            ? "neutral"
                            : "warning"
                      }
                    >
                      {d.jdStatus}
                    </Chip>
                  </td>
                  <td className="num">{d.matches || "—"}</td>
                  <td>{d.bestBand ? <BandChip band={d.bestBand} /> : "—"}</td>
                  <td className="row" style={{ flexWrap: "nowrap" }}>
                    <button className="btn btn-sm" onClick={() => setOpen(d)}>
                      {d.jdStatus === "Not analysed"
                        ? "Analyze JD"
                        : "Review JD"}
                    </button>
                    {d.matches > 0 && (
                      <Link
                        className="btn btn-sm btn-tertiary"
                        to={`/matches?req=${encodeURIComponent(d.codeRequisition)}`}
                      >
                        View matches
                      </Link>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && <JdReview demand={open} onClose={() => setOpen(null)} />}
      {upload && <UploadDialog kind="dl" onClose={() => setUpload(false)} />}
    </div>
  );
}
