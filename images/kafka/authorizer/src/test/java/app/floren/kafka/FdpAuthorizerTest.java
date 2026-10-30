package app.floren.kafka;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import org.apache.kafka.common.acl.AclOperation;
import org.apache.kafka.common.resource.*;
import org.apache.kafka.server.authorizer.Action;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class FdpAuthorizerTest {
    @Test void authenticatedDecisionsFailClosedAndRereadRotatedCredentials() throws Exception {
        String token = "a".repeat(64);
        var file = Files.createTempFile("fdp-policy", ".token");
        Files.writeString(file, token);
        AtomicReference<String> reply = new AtomicReference<>("{\"result\":true}");
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/allow", exchange -> {
            assertEquals("Bearer " + token, exchange.getRequestHeaders().getFirst("Authorization"));
            String body = new String(exchange.getRequestBody().readAllBytes());
            assertTrue(body.contains("s_session"));
            byte[] response = reply.get().getBytes();
            exchange.sendResponseHeaders(200, response.length);
            exchange.getResponseBody().write(response); exchange.close();
        });
        server.start();
        try {
            var authorizer = new FdpAuthorizer();
            authorizer.configure(Map.of("fdp.policy.url", "http://127.0.0.1:"+server.getAddress().getPort()+"/allow", "fdp.policy.token.file", file.toString()));
            var action = new Action(AclOperation.READ, new ResourcePattern(ResourceType.TOPIC, "orders", PatternType.LITERAL), 1, true, true);
            assertTrue(authorizer.allow("User", "s_session", action));
            reply.set("{\"result\":\"true\"}"); assertFalse(authorizer.allow("User", "s_session", action));
            reply.set("{\"result\":false}"); assertFalse(authorizer.allow("User", "s_session", action));
            reply.set("x".repeat(5000)); assertFalse(authorizer.allow("User", "s_session", action));
            reply.set("{malformed"); assertFalse(authorizer.allow("User", "s_session", action));
            assertFalse(authorizer.allow("User", "admin", action));
            Files.writeString(file, "invalid"); assertFalse(authorizer.allow("User", "s_session", action));
            Files.writeString(file, token); server.stop(0);
            assertFalse(authorizer.allow("User", "s_session", action));
        } finally { server.stop(0); Files.deleteIfExists(file); }
    }
}
