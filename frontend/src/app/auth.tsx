import { Typography } from "@heroui/react";
import { createContext, use, useMemo, useState, type ReactNode } from "react";
import { Outlet } from "react-router";
import { sameOriginClient, type ApiClient } from "../api/client";
import { EmptyView } from "../components/molecules/EmptyView";

interface AccessContextValue {
  client: ApiClient;
  /** Whether the API answered 401: the key the dev proxy attaches is missing or not accepted. */
  rejected: boolean;
}

const AccessContext = createContext<AccessContextValue | null>(null);

/** The API client for the screens. The browser holds no key: the server that serves the UI attaches one (UI), so a 401 can only be fixed there, and the content area shows how until the page is reloaded. */
export function AccessProvider({ children }: Readonly<{ children: ReactNode }>) {
  const [rejected, setRejected] = useState(false);
  const client = useMemo(() => {
    const created = sameOriginClient();
    created.use({
      onResponse({ response }) {
        // RFC 9110 §15.5.2: 401 means the request's credentials were not accepted (missing, unknown or
        // revoked).
        if (response.status === 401) setRejected(true);
      },
    });
    return created;
  }, []);
  const value = useMemo(() => ({ client, rejected }), [client, rejected]);
  return <AccessContext value={value}>{children}</AccessContext>;
}

function useAccess(): AccessContextValue {
  const access = use(AccessContext);
  if (access === null) throw new Error("useApiClient is used outside AccessProvider");
  return access;
}

/** The API client of this page's origin. A 401 answer makes `RequireAccess` show the setup notice. */
export function useApiClient(): ApiClient {
  return useAccess().client;
}

/** Route element for the screens: the screen, or the setup notice in its place once the API has answered 401. */
export function RequireAccess() {
  if (!useAccess().rejected) return <Outlet />;
  return (
    <EmptyView title="The API rejected the request.">
      Set <Typography.Code>SNOWCRAWL_UI_API_KEY</Typography.Code> in{" "}
      <Typography.Code>.env</Typography.Code> to a key from{" "}
      <Typography.Code>snowcrawl apikey create</Typography.Code>, then restart{" "}
      <Typography.Code>pnpm dev</Typography.Code>.
    </EmptyView>
  );
}
