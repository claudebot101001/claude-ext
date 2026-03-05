// Stealth evasions injected before page load via addInitScript.
// Patches WebGL renderer info, WebRTC IP leak, and other fingerprint vectors.
(() => {
  // --- WebGL Renderer Spoofing ---
  // Override SwiftShader with a realistic GPU string.
  const SPOOFED_VENDOR = "Intel Inc.";
  const SPOOFED_RENDERER =
    "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92), OpenGL 4.6)";

  const origGetParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (param) {
    // UNMASKED_VENDOR_WEBGL = 0x9245, UNMASKED_RENDERER_WEBGL = 0x9246
    if (param === 0x9245) return SPOOFED_VENDOR;
    if (param === 0x9246) return SPOOFED_RENDERER;
    return origGetParameter.call(this, param);
  };

  // Also patch WebGL2
  if (typeof WebGL2RenderingContext !== "undefined") {
    const origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (param) {
      if (param === 0x9245) return SPOOFED_VENDOR;
      if (param === 0x9246) return SPOOFED_RENDERER;
      return origGetParameter2.call(this, param);
    };
  }

  // --- WebRTC IP Leak Prevention ---
  // Replace RTCPeerConnection to prevent local IP discovery.
  if (typeof RTCPeerConnection !== "undefined") {
    const OrigRTC = RTCPeerConnection;
    window.RTCPeerConnection = function (...args) {
      const config = args[0] || {};
      // Force relay-only to prevent IP leak via STUN
      config.iceTransportPolicy = "relay";
      return new OrigRTC(config);
    };
    window.RTCPeerConnection.prototype = OrigRTC.prototype;
    Object.defineProperty(window, "RTCPeerConnection", {
      writable: false,
      configurable: false,
    });
  }

  // --- Hardware Concurrency ---
  // Normalize to common value (real machines usually have 4-16)
  Object.defineProperty(navigator, "hardwareConcurrency", {
    get: () => 8,
  });

  // --- Device Memory ---
  // Normalize to common value
  if ("deviceMemory" in navigator) {
    Object.defineProperty(navigator, "deviceMemory", {
      get: () => 8,
    });
  }
})();
