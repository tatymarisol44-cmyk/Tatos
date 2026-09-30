package dev.agency.jvmagent.a2a;

import dev.agency.jvmagent.AgentProperties;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ReadListener;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletInputStream;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * Caps request bodies on {@code /a2a}. Spring limits form posts, not JSON, so without this a
 * single request could make the agent buffer an arbitrarily large body. Declared lengths over
 * the cap get 413 up front; chunked bodies are cut off while being read.
 */
@Component
public class BodySizeLimitFilter extends OncePerRequestFilter {

    /** Raised when a streamed body passes the cap; mapped to HTTP 413 by the controller. */
    public static class PayloadTooLargeException extends IOException {
        PayloadTooLargeException(long limit) {
            super("request body exceeds " + limit + " bytes");
        }
    }

    private final long maxBytes;

    public BodySizeLimitFilter(AgentProperties props) {
        this.maxBytes = props.maxBodyBytes();
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        return !"/a2a".equals(request.getRequestURI());
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        if (request.getContentLengthLong() > maxBytes) {
            response.sendError(HttpServletResponse.SC_REQUEST_ENTITY_TOO_LARGE);
            return;
        }
        chain.doFilter(new Limited(request, maxBytes), response);
    }

    private static final class Limited extends HttpServletRequestWrapper {
        private final long max;
        private ServletInputStream stream;

        Limited(HttpServletRequest request, long max) {
            super(request);
            this.max = max;
        }

        @Override
        public ServletInputStream getInputStream() throws IOException {
            if (stream == null) {
                stream = new CountingStream(super.getInputStream(), max);
            }
            return stream;
        }
    }

    private static final class CountingStream extends ServletInputStream {
        private final ServletInputStream in;
        private final long max;
        private long count;

        CountingStream(ServletInputStream in, long max) {
            this.in = in;
            this.max = max;
        }

        private int check(int n) throws IOException {
            if (n > 0) {
                count += n;
                if (count > max) {
                    throw new PayloadTooLargeException(max);
                }
            }
            return n;
        }

        @Override
        public int read() throws IOException {
            int b = in.read();
            if (b >= 0) {
                check(1);
            }
            return b;
        }

        @Override
        public int read(byte[] buf, int off, int len) throws IOException {
            return check(in.read(buf, off, len));
        }

        @Override
        public boolean isFinished() {
            return in.isFinished();
        }

        @Override
        public boolean isReady() {
            return in.isReady();
        }

        @Override
        public void setReadListener(ReadListener listener) {
            in.setReadListener(listener);
        }
    }
}
