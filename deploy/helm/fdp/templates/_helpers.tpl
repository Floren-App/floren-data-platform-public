{{- define "fdp.secretNamespace" -}}
{{- $namespace := default (printf "%s-secrets" .Release.Namespace) .Values.secretManagement.namespace -}}
{{- if or (gt (len $namespace) 63) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $namespace)) -}}
{{- fail "secretManagement.namespace must be a valid Kubernetes namespace (at most 63 characters)" -}}
{{- end -}}
{{- $namespace -}}
{{- end -}}
{{- define "fdp.issuer" -}}
{{- if .Values.keycloak.enabled -}}
{{- printf "%s/realms/%s" (trimSuffix "/" .Values.keycloak.publicUrl) .Values.keycloak.realm -}}
{{- else -}}{{- .Values.oidc.issuer -}}{{- end -}}
{{- end -}}
{{- define "fdp.installation" -}}
{{- printf "%s/%s" .Release.Namespace .Release.Name | sha256sum | trunc 20 -}}
{{- end -}}
{{- define "fdp.layerNamespacePrefix" -}}
{{- printf "fdp-%s-layer" (include "fdp.installation" .) -}}
{{- end -}}
{{- define "fdp.imagePullSecrets" -}}
{{- if .Values.registryCredentials.existingSecret }}
imagePullSecrets:
  - name: {{ .Values.registryCredentials.existingSecret | quote }}
{{- end }}
{{- end -}}

{{- /* Where the app client may send people back after sign-in: the desktop app, and the browser app at
       publicUrl when it is served. Used by the realm import and the post-upgrade client update. */}}
{{- define "fdp.appRedirectUris" -}}
{{- $uris := .Values.keycloak.desktopRedirectUris -}}
{{- if .Values.web.enabled -}}
{{- $uris = append $uris (printf "%s/*" (trimSuffix "/" .Values.publicUrl)) -}}
{{- end -}}
{{- toJson $uris -}}
{{- end -}}

{{- define "fdp.databaseNamespace" -}}
{{- $ns := default .Release.Namespace .Values.metadataDatabase.namespace -}}
{{- if or (gt (len $ns) 63) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $ns)) -}}
{{- fail "metadataDatabase.namespace must be a valid Kubernetes namespace" -}}
{{- end -}}
{{- if or (eq $ns .Values.runtime.namespace) (eq $ns (include "fdp.secretNamespace" .)) -}}
{{- fail "metadataDatabase.namespace must differ from runtime and secret namespaces" -}}
{{- end -}}
{{- $ns -}}
{{- end -}}
{{- define "fdp.databaseHost" -}}
{{- if eq (include "fdp.databaseNamespace" .) .Release.Namespace -}}fdp-metadata-db
{{- else -}}{{- printf "fdp-metadata-db.%s.svc" (include "fdp.databaseNamespace" .) -}}
{{- end -}}
{{- end -}}
{{- define "fdp.openbaoNamespace" -}}
{{- $namespace := default (printf "%s-openbao" .Release.Namespace) .Values.openbao.namespace -}}
{{- if or (gt (len $namespace) 63) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $namespace)) -}}
{{- fail "openbao.namespace must be a valid Kubernetes namespace (at most 63 characters)" -}}
{{- end -}}
{{- if has $namespace (list .Release.Namespace .Values.runtime.namespace (include "fdp.secretNamespace" .)) -}}
{{- fail "openbao.namespace must be dedicated to OpenBao, separate from platform, runtime and secret namespaces" -}}
{{- end -}}
{{- $namespace -}}
{{- end -}}
{{- define "fdp.openbaoScheme" -}}
{{- if eq .Values.openbao.tls.mode "disabled" }}http{{ else }}https{{ end -}}
{{- end -}}
{{- define "fdp.openbaoAddr" -}}
{{- if .Values.openbao.enabled -}}
{{- printf "%s://fdp-openbao.%s.svc:8200" (include "fdp.openbaoScheme" .) (include "fdp.openbaoNamespace" .) -}}
{{- else -}}
{{- .Values.openbao.addr -}}
{{- end -}}
{{- end -}}

{{/* "true" when the layer controller writes CiliumNetworkPolicies (layerController.network.cilium: auto detects Cilium). */}}
{{- define "fdp.layerCilium" -}}
{{- $cilium := toString (dig "cilium" "auto" .Values.layerController.network) -}}
{{- if or (eq $cilium "true") (and (eq $cilium "auto") (.Capabilities.APIVersions.Has "cilium.io/v2/CiliumNetworkPolicy")) -}}true{{- end -}}
{{- end -}}
