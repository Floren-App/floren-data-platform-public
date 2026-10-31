// Minimal stroke icons in the spirit of VS Code's codicons.
type P = { size?: number };

const svg = (size: number, children: React.ReactNode) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6}
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {children}
  </svg>
);

export const IconWorkspaces = ({ size = 24 }: P) =>
  svg(size, <><rect x="3" y="4" width="18" height="14" rx="2" /><path d="M8 21h8M7 9l3 3-3 3M13 15h4" /></>);
export const IconRuns = ({ size = 24 }: P) =>
  svg(size, <><circle cx="12" cy="12" r="9" /><path d="M10 8.5v7l5.5-3.5z" /></>);
export const IconData = ({ size = 24 }: P) =>
  svg(size, <><ellipse cx="12" cy="5.5" rx="7" ry="2.5" /><path d="M5 5.5v13c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-13M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5" /></>);
export const IconStream = ({ size = 24 }: P) =>
  svg(size, <><circle cx="8" cy="12" r="2.5" /><circle cx="8" cy="4.5" r="2" /><circle cx="8" cy="19.5" r="2" /><circle cx="17" cy="8" r="2" /><circle cx="17" cy="16" r="2" /><path d="M8 6.5v3M8 14.5v3m2.3-6.6 4.9-2.1m-4.9 4.4 4.9 2.1" /></>);
export const IconCompute = ({ size = 24 }: P) =>
  svg(size, <><rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" /><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" /></>);
export const IconAdmin = ({ size = 24 }: P) =>
  svg(size, <><path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z" /><path d="M9 12l2 2 4-4" /></>);
export const IconAccount = ({ size = 24 }: P) =>
  svg(size, <><circle cx="12" cy="8" r="4" /><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6" /></>);
export const IconHome = ({ size = 24 }: P) =>
  svg(size, <><path d="M3 11l9-8 9 8M5 10v11h14V10M9 21v-8h6v8" /></>);
export const IconBook = ({ size = 24 }: P) =>
  svg(size, <><path d="M12 6C9 4 5 4 3 5v15c2-1 6-1 9 1 3-2 7-2 9-1V5c-2-1-6-1-9 1zM12 6v15" /></>);
export const IconDeployed = ({ size = 24 }: P) =>
  svg(size, <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><path d="M14 14h7v7h-7zM16 16l3 1.5-3 1.5z" /></>);
export const IconBranch = ({ size = 16 }: P) =>
  svg(size, <><circle cx="6" cy="5" r="2" /><circle cx="6" cy="19" r="2" /><circle cx="18" cy="7" r="2" /><path d="M6 7v10M18 9c0 5-6 4-11.5 8.5" /></>);
export const IconPackage = ({ size = 24 }: P) =>
  svg(size, <><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" /><path d="M4 7.5l8 4.5 8-4.5M12 12v9M8 5.2l8 4.6" /></>);
export const IconImport = ({ size = 24 }: P) =>
  svg(size, <><path d="M12 3v11M7.5 9.5L12 14l4.5-4.5" /><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" /></>);
export const IconKey = ({ size = 24 }: P) =>
  svg(size, <><circle cx="8" cy="15" r="4" /><path d="M11 12l9-9M16 7l3 3M14 9l2 2" /></>);
export const IconPlus = ({ size = 16 }: P) => svg(size, <path d="M12 5v14M5 12h14" />);
export const IconPlay = ({ size = 16 }: P) => svg(size, <path d="M7 5v14l11-7z" />);
export const IconStop = ({ size = 16 }: P) => svg(size, <rect x="6" y="6" width="12" height="12" rx="1" />);
export const IconOpen = ({ size = 16 }: P) =>
  svg(size, <path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />);
export const IconTrash = ({ size = 16 }: P) =>
  svg(size, <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />);
export const IconClose = ({ size = 14 }: P) => svg(size, <path d="M6 6l12 12M18 6L6 18" />);
export const IconPin = ({ size = 14 }: P) =>
  svg(size, <><path d="M7 3h10l-2 5v3l3 3H6l3-3V8zM12 14v7" /></>);
export const IconLogs = ({ size = 16 }: P) => svg(size, <path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5" />);
export const IconRefresh = ({ size = 16 }: P) =>
  svg(size, <path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" />);
export const IconSettings = ({ size = 24 }: P) =>
  svg(size, <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></>);
export const IconCatalog = ({ size = 24 }: P) =>
  svg(size, <><path d="M4 5h9M4 10h6M4 15h4M4 20h8" /><circle cx="16.5" cy="13.5" r="3.5" /><path d="M19 16l2.5 2.5" /></>);
export const IconChevron = ({ size = 14 }: P) => svg(size, <path d="M10 7l5 5-5 5" />);
export const IconMore = ({ size = 16 }: P) =>
  svg(size, <><circle cx="5" cy="12" r="1" fill="currentColor" /><circle cx="12" cy="12" r="1" fill="currentColor" /><circle cx="19" cy="12" r="1" fill="currentColor" /></>);
export const IconApi = ({ size = 24 }: P) =>
  svg(size, <><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9s1.3-6.4 3.8-9z" /></>);
export const IconLog = ({ size = 24 }: P) =>
  svg(size, <><path d="M5 4h14v16H5z" /><path d="M8 8h8M8 12h8M8 16h5" /></>);
