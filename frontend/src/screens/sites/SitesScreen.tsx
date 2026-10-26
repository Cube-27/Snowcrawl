import { Card } from "@heroui/react";
import { useNavigate } from "react-router";
import { LookupForm } from "../../components/molecules/LookupForm";
import { Screen } from "../../components/templates/Screen";
import { sitePath } from "./paths";

/** Opens a site's configs by host. The API reads configs per host (`GET /v1/sites/{host}/configs`) and has no list
 * of sites, so the host is entered here. */
export function SitesScreen() {
  const navigate = useNavigate();
  return (
    <Screen
      title="Sites"
      description="Each site keeps its learned configs: review them, correct a field, or learn again."
    >
      <Card>
        <Card.Header>
          <Card.Title>Open a site</Card.Title>
          <Card.Description>
            Enter the site's host name, as its records name it, or any page URL of the site.
          </Card.Description>
        </Card.Header>
        <Card.Content>
          <LookupForm
            label="Host"
            placeholder="shop.example.com"
            submitLabel="Open site"
            onOpen={(value) => void navigate(sitePath(hostOf(value)))}
          />
        </Card.Content>
      </Card>
    </Screen>
  );
}

// RFC 3986 section 3.2: a URL's authority follows "//"; its host is what remains without userinfo ("...@") and port.
const AUTHORITY_HOST = /^[a-z][a-z0-9+.-]*:\/\/(?:[^@/?#]*@)?(\[[^\]/?#]*\]|[^:/?#]*)/i;

/** The host a value names: a URL's host read as the backend reads a page URL's (Python `urlsplit().hostname`:
 * lowercased, no brackets, no port, an internationalized name kept as written, not converted to punycode as the WHATWG
 * URL parser does), else the value itself, taken as a host. */
function hostOf(value: string): string {
  const host = AUTHORITY_HOST.exec(value)?.[1];
  return host ? host.replace(/^\[|\]$/g, "").toLowerCase() : value;
}
