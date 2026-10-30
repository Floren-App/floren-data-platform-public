package app.floren.kafka;

import java.util.List;
import java.util.Map;
import java.util.Set;
import javax.security.auth.callback.Callback;
import javax.security.auth.callback.UnsupportedCallbackException;
import javax.security.auth.login.AppConfigurationEntry;
import org.apache.kafka.common.security.auth.AuthenticateCallbackHandler;
import org.apache.kafka.common.security.oauthbearer.OAuthBearerToken;
import org.apache.kafka.common.security.oauthbearer.OAuthBearerTokenCallback;

/** Server-only JAAS subject for the external listener, never used for inter-broker login.
 * This is not a token validator. Strimzi's separate server validation callback still
 * requires a signed FDP JWT. This non-JWT value cannot authenticate a network client.
 */
public final class FdpListenerLogin implements AuthenticateCallbackHandler {
    @Override public void configure(Map<String, ?> configs, String mechanism, List<AppConfigurationEntry> entries) {
        if (!"OAUTHBEARER".equals(mechanism)) throw new IllegalArgumentException("OAUTHBEARER only");
    }
    @Override public void handle(Callback[] callbacks) throws UnsupportedCallbackException {
        for (Callback callback : callbacks) {
            if (!(callback instanceof OAuthBearerTokenCallback token)) throw new UnsupportedCallbackException(callback);
            final long now = System.currentTimeMillis();
            token.token(new OAuthBearerToken() {
                public String value() { return "server-listener-not-a-client-token"; }
                public Set<String> scope() { return Set.of(); }
                public long lifetimeMs() { return now + 86400000L; }
                public String principalName() { return "server-listener"; }
                public Long startTimeMs() { return now; }
            });
        }
    }
    @Override public void close() { }
}
