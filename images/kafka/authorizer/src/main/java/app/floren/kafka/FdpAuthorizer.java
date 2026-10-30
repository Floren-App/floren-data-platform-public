package app.floren.kafka;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.io.ByteArrayOutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import org.apache.kafka.common.Endpoint;
import org.apache.kafka.common.acl.*;
import org.apache.kafka.common.errors.UnsupportedVersionException;
import org.apache.kafka.server.authorizer.*;

/** OPA-shaped remote decisions with authenticated broker callbacks and no fail-open/cache path. */
public final class FdpAuthorizer implements Authorizer {
    private final ObjectMapper json = new ObjectMapper();
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    private URI endpoint;
    private Path tokenFile;
    private Set<String> superUsers = Set.of();

    @Override public void configure(Map<String, ?> config) {
        endpoint = URI.create(Objects.toString(config.get("fdp.policy.url")));
        if (!Set.of("http", "https").contains(endpoint.getScheme()) || endpoint.getHost() == null || endpoint.getUserInfo() != null)
            throw new IllegalArgumentException("FDP policy endpoint must be HTTP(S), without embedded credentials");
        tokenFile = Path.of(Objects.toString(config.get("fdp.policy.token.file")));
        String users = Objects.toString(config.get("super.users"), "");
        superUsers = new HashSet<>(Arrays.asList(users.split(";")));
    }

    @Override public Map<Endpoint, ? extends CompletionStage<Void>> start(AuthorizerServerInfo info) {
        Map<Endpoint, CompletionStage<Void>> result = new HashMap<>();
        for (Endpoint endpoint : info.endpoints()) result.put(endpoint, CompletableFuture.completedFuture(null));
        return result;
    }

    boolean allow(String type, String principal, Action action) {
        try {
            if (!"User".equals(type) || !principal.startsWith("s_") || principal.length() > 64) return false;
            String token = Files.readString(tokenFile).trim();
            if (!token.matches("[a-f0-9]{64}")) return false;
            Map<String, Object> input = Map.of(
                "requestContext", Map.of("principal", Map.of("name", principal, "principalType", type)),
                "action", Map.of("operation", action.operation().name(), "resourcePattern", Map.of(
                    "name", action.resourcePattern().name(), "resourceType", action.resourcePattern().resourceType().name(),
                    "patternType", action.resourcePattern().patternType().name(), "unknown", false)));
            HttpRequest request = HttpRequest.newBuilder(endpoint).timeout(Duration.ofSeconds(3))
                .header("Content-Type", "application/json").header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(Map.of("input", input)))).build();
            var pending = http.sendAsync(request, info -> new LimitedBody());
            try {
                HttpResponse<byte[]> response = pending.get(3, TimeUnit.SECONDS);
                if (response.statusCode() != 200) return false;
                var result = json.readTree(response.body()).path("result");
                return result.isBoolean() && result.booleanValue();
            } finally {
                if (!pending.isDone()) pending.cancel(true);
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            return false;
        } catch (Exception ignored) {
            // Never log the callback token, principal session or raw endpoint/error body.
            return false;
        }
    }

    /** Bound memory as well as time, including a backend that never finishes its response. */
    private static final class LimitedBody implements HttpResponse.BodySubscriber<byte[]> {
        private final CompletableFuture<byte[]> result = new CompletableFuture<>();
        private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        private Flow.Subscription subscription;
        @Override public CompletionStage<byte[]> getBody() { return result; }
        @Override public void onSubscribe(Flow.Subscription subscription) { this.subscription = subscription; subscription.request(1); }
        @Override public void onNext(List<ByteBuffer> buffers) {
            for (ByteBuffer buffer : buffers) {
                if (buffer.remaining() > 4096 - bytes.size()) {
                    subscription.cancel(); result.completeExceptionally(new IllegalArgumentException("Oversized policy response")); return;
                }
                byte[] part = new byte[buffer.remaining()]; buffer.get(part); bytes.writeBytes(part);
            }
            subscription.request(1);
        }
        @Override public void onError(Throwable error) { result.completeExceptionally(error); }
        @Override public void onComplete() { result.complete(bytes.toByteArray()); }
    }

    @Override public List<AuthorizationResult> authorize(AuthorizableRequestContext context, List<Action> actions) {
        boolean internal = superUsers.contains(context.principal().toString());
        List<AuthorizationResult> result = new ArrayList<>(actions.size());
        for (Action action : actions) result.add(internal || allow(context.principal().getPrincipalType(), context.principal().getName(), action)
            ? AuthorizationResult.ALLOWED : AuthorizationResult.DENIED);
        return result;
    }

    @Override public List<? extends CompletionStage<AclCreateResult>> createAcls(AuthorizableRequestContext context, List<AclBinding> acls) {
        return acls.stream().map(a -> CompletableFuture.completedFuture(new AclCreateResult(
            new UnsupportedVersionException("Manage permissions through FDP")))).toList();
    }
    @Override public List<? extends CompletionStage<AclDeleteResult>> deleteAcls(AuthorizableRequestContext context, List<AclBindingFilter> filters) {
        return filters.stream().map(a -> CompletableFuture.completedFuture(new AclDeleteResult(
            new UnsupportedVersionException("Manage permissions through FDP")))).toList();
    }
    @Override public Iterable<AclBinding> acls(AclBindingFilter filter) { return List.of(); }
    @Override public void close() { }
}
