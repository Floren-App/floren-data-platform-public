import { ApiError, DataAccessIssue } from "../api";
import { OpenTab } from "./Permissions";

/** Permission failures explain both the missing grant and the identity receiving it. */
export function AccessError({ error, issue, openTab }: {
  error: string | Error; issue?: DataAccessIssue | null; openTab: OpenTab;
}) {
  if(error instanceof ApiError&&error.slaIssue)return <section className="card access-error" role="alert">
    <h3>Upstream SLA requires attention</h3><p className="error">{error.slaIssue.message}</p><div className="form-actions">{error.slaIssue.assets.map(a=><button key={a.asset_id} className="btn secondary" onClick={()=>openTab({kind:'catalog-object',ref:a.asset_id,title:a.name||'Required input',catalogSection:'sla'})}>{a.name||'Required input'} · {a.status.replaceAll('_',' ')}</button>)}</div>
  </section>;
  const detail = issue ?? (error instanceof ApiError ? error.accessIssue : undefined);
  if (!detail) return <p className="error" role="alert">{typeof error === "string" ? error : error.message}</p>;
  const serviceId = detail.identity.kind === "service" ? detail.identity.principal_id ?? undefined : undefined;
  return <section className="card access-error" role="alert">
    <h3>Data access required</h3>
    <p className="error">{detail.message}</p>
    <p>{detail.guidance}</p>
    <div className="form-actions">
      {detail.sources.filter((s) => s.resource_id).map((source) => <button key={source.resource_id!} className="btn secondary"
        onClick={() => openTab({ kind: "source", ref: source.name, title: source.name, section: "permissions", grantPrincipalId: serviceId })}>
        {source.name} permissions{!source.can_assign && " (view)"}
      </button>)}
      {detail.sources.flatMap((source) => (source.schemas ?? []).map((schema) => <button key={schema.id} className="btn secondary"
        onClick={() => openTab({ kind: "schema", ref: schema.id, title: schema.name, section: "permissions", grantPrincipalId: serviceId })}>
        {source.name} / {schema.name} permissions{!schema.can_assign && " (view)"}
      </button>))}
      {detail.can_manage_groups && <button className="btn secondary" onClick={() => openTab({ kind: "access", title: "Groups", bindingPrincipalId: detail.identity.principal_id ?? undefined })}>Manage identity bindings</button>}
    </div>
    {(!detail.sources.length || detail.sources.some((s) => !s.can_assign)) && <p className="hint">A data source or schema Owner must assign the data role. A designated group owner manages identity bindings.</p>}
  </section>;
}
