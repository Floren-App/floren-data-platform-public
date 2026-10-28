import type { ReactNode } from "react";

/**
 * The one page header: identity on the left (icon, title, subtitle), page-level actions on the right.
 * Put the primary action last in `actions`; destructive actions go after a `<span className="divider" />`.
 */
export function PageHeader({ icon, title, subtitle, actions, children }: {
  icon?: ReactNode; title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children?: ReactNode;
}) {
  return <header className="page-header">
    <div className="page-header-main">
      <h1 className="page-title">{icon}{title}</h1>
      {subtitle && <p className="page-subtitle">{subtitle}</p>}
      {children}
    </div>
    {actions && <div className="page-actions">{actions}</div>}
  </header>;
}
