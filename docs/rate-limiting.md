# 🚦 Rate Limiting

To maintain service stability, prevent brute-forcing, and defend against denial-of-service attempts, VelocityRL employs a custom, high-performance in-memory **Sliding Window Rate Limiter**.

---

## Limits & Windows

The rate limiter is applied globally across all API endpoints:

| Property | Value | Description |
| :--- | :--- | :--- |
| **Max Requests** | `60` | Maximum allowable successful queries per IP within the window. |
| **Time Window** | `60s` | Timeframe in seconds for the sliding window bucket. |
| **Scope** | Client IP | Rate limiting is calculated uniquely per client IP address. |

---

## Response Headers

Every HTTP response returned by the API—including successful `200 OK` requests and blocked `429 Too Many Requests` responses—contains active HTTP tracking headers:

```http
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 59
X-RateLimit-Reset: 60
```

* **`X-RateLimit-Limit`**: The maximum number of requests allowed inside the active window.
* **`X-RateLimit-Remaining`**: The number of requests remaining inside your active window.
* **`X-RateLimit-Reset`**: The exact number of seconds until your sliding window bucket completely clears and resets back to maximum capacity.

---

## Memory-Safe Garbage Collection

Unlike naive in-memory rate limiters that cause memory leaks over time by storing infinite IP history blocks, the VelocityRL rate limiter runs a periodic cleanup daemon:
* **Sweep Frequency**: Every 300 seconds (5 minutes), the rate-limiter sweeps its tracking dictionaries.
* **Pruning Strategy**: Completely purges inactive IP histories from system RAM, ensuring stable, low-overhead memory consumption on production nodes.

---

## Rate Limit Reached Response

When a client IP exceeds 60 requests in the active rolling window, the API immediately halts request execution and yields a lightweight, standard JSON structure with HTTP status code `429 Too Many Requests`:

```http
HTTP/2 429 Too Many Requests
Content-Type: application/json
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 42

{
  "detail": "Too Many Requests. Please slow down (limit: 60 requests/min)."
}
```
