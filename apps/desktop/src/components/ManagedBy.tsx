import { useEffect, useState } from "react";
import { api, Provenance } from "../api";

/** Who manages this object's configuration. For Terraform-managed objects it warns before an edit here drifts from
 * the configuration (the next apply may undo it), and says when that has already happened. Emergency changes stay
 * possible; this only explains. Unknown provenance shows nothing: none is invented. */
export function ManagedBy({ target }: { target: string }) {
  const [p, setP] = useState<Provenance | null>(null);
  useEffect(() => {
    let live = true;
    setP(null);
    api.provenance(target).then((r) => live && setP(r)).catch(() => live && setP(null));
    return () => { live = false; };
  }, [target]);
  if (!p || p.source !== "terraform") return null;
  return <div className="managed-by" role="note" aria-label="Managed by Terraform">
    <strong>Managed by Terraform</strong>{p.reference && <> in <code>{p.reference}</code></>}{p.revision && <> at <code>{p.revision}</code></>}.
    {p.app_change
      ? <> Changed here by {p.app_change.by} on {new Date(p.app_change.at).toLocaleString()} since Terraform last applied it: the next plan will likely show this as drift. Update the configuration, or the next apply undoes it.</>
      : <> Change it in the configuration: a change made here may be undone by the next apply.</>}
  </div>;
}
