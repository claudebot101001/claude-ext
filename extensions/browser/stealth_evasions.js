// Stealth evasions injected before page load via addInitScript.
// Patches fingerprint vectors: WebGL, WebRTC, Canvas, Audio, Screen, Timezone, etc.
(() => {
  const CFG = window.__STEALTH_CONFIG__ || {};

  // --- Seeded PRNG (mulberry32) ---
  function mulberry32(seed) {
    let s = seed | 0;
    return function () {
      s = (s + 0x6d2b79f5) | 0;
      let t = Math.imul(s ^ (s >>> 15), 1 | s);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  const canvasRng = mulberry32(CFG.canvas_seed || 42);
  const audioRng = mulberry32(CFG.audio_seed || 42);

  // --- WebGL Renderer Spoofing ---
  const SPOOFED_VENDOR = CFG.webgl_vendor || "Intel Inc.";
  const SPOOFED_RENDERER =
    CFG.webgl_renderer ||
    "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92), OpenGL 4.6)";

  const origGetParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (param) {
    if (param === 0x9245) return SPOOFED_VENDOR;
    if (param === 0x9246) return SPOOFED_RENDERER;
    return origGetParameter.call(this, param);
  };

  if (typeof WebGL2RenderingContext !== "undefined") {
    const origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (param) {
      if (param === 0x9245) return SPOOFED_VENDOR;
      if (param === 0x9246) return SPOOFED_RENDERER;
      return origGetParameter2.call(this, param);
    };
  }

  // --- WebRTC IP Leak Prevention ---
  if (typeof RTCPeerConnection !== "undefined") {
    const OrigRTC = RTCPeerConnection;
    window.RTCPeerConnection = function (...args) {
      const config = args[0] || {};
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
  Object.defineProperty(navigator, "hardwareConcurrency", {
    get: () => CFG.hardware_concurrency || 8,
  });

  // --- Device Memory ---
  if ("deviceMemory" in navigator) {
    Object.defineProperty(navigator, "deviceMemory", {
      get: () => CFG.device_memory || 8,
    });
  }

  // --- Canvas 2D Fingerprinting ---
  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function (...args) {
    const ctx = this.getContext("2d");
    if (ctx) {
      const w = this.width;
      const h = this.height;
      if (w > 0 && h > 0) {
        try {
          const imageData = origGetImageData.call(ctx, 0, 0, w, h);
          const d = imageData.data;
          for (let i = 0; i < d.length; i += 4) {
            d[i] = (d[i] + Math.floor(canvasRng() * 3) - 1) & 0xff;
          }
          ctx.putImageData(imageData, 0, 0);
        } catch (e) {
          // CORS or empty canvas
        }
      }
    }
    return origToDataURL.apply(this, args);
  };

  const origToBlob = HTMLCanvasElement.prototype.toBlob;
  HTMLCanvasElement.prototype.toBlob = function (callback, ...args) {
    const ctx = this.getContext("2d");
    if (ctx) {
      const w = this.width;
      const h = this.height;
      if (w > 0 && h > 0) {
        try {
          const imageData = origGetImageData.call(ctx, 0, 0, w, h);
          const d = imageData.data;
          for (let i = 0; i < d.length; i += 4) {
            d[i] = (d[i] + Math.floor(canvasRng() * 3) - 1) & 0xff;
          }
          ctx.putImageData(imageData, 0, 0);
        } catch (e) {
          // CORS or empty canvas
        }
      }
    }
    return origToBlob.call(this, callback, ...args);
  };

  const origGetImageData =
    CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function (...args) {
    const imageData = origGetImageData.apply(this, args);
    const d = imageData.data;
    for (let i = 0; i < d.length; i += 4) {
      d[i] = (d[i] + Math.floor(canvasRng() * 3) - 1) & 0xff;
    }
    return imageData;
  };

  // --- AudioContext Fingerprinting ---
  if (typeof OfflineAudioContext !== "undefined") {
    const origStartRendering =
      OfflineAudioContext.prototype.startRendering;
    OfflineAudioContext.prototype.startRendering = function () {
      return origStartRendering.call(this).then(function (buffer) {
        for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
          const data = buffer.getChannelData(ch);
          for (let i = 0; i < data.length; i++) {
            data[i] += (audioRng() - 0.5) * 0.0002;
          }
        }
        return buffer;
      });
    };
  }

  // --- Screen Dimensions ---
  const screenProps = {
    width: CFG.screen_width || 1920,
    height: CFG.screen_height || 1080,
    availWidth: CFG.screen_avail_width || CFG.screen_width || 1920,
    availHeight: CFG.screen_avail_height || CFG.screen_height
      ? (CFG.screen_height || 1080) - 40
      : 1040,
    colorDepth: CFG.screen_color_depth || 24,
  };
  for (const [prop, val] of Object.entries(screenProps)) {
    Object.defineProperty(screen, prop, { get: () => val });
  }
  // Also override pixelDepth to match colorDepth
  Object.defineProperty(screen, "pixelDepth", {
    get: () => screenProps.colorDepth,
  });

  // --- Timezone Spoofing ---
  const spoofedTimezone = CFG.timezone || "America/New_York";

  // Map common timezones to UTC offsets (in minutes, as getTimezoneOffset returns)
  const tzOffsets = {
    "America/New_York": 300,
    "America/Chicago": 360,
    "America/Denver": 420,
    "America/Los_Angeles": 480,
    "America/Anchorage": 540,
    "Pacific/Honolulu": 600,
    "Europe/London": 0,
    "Europe/Paris": -60,
    "Europe/Berlin": -60,
    "Europe/Moscow": -180,
    "Asia/Tokyo": -540,
    "Asia/Shanghai": -480,
    "Asia/Kolkata": -330,
    "Australia/Sydney": -660,
    "Pacific/Auckland": -720,
  };
  const spoofedOffset =
    CFG.timezone_offset !== undefined
      ? CFG.timezone_offset
      : tzOffsets[spoofedTimezone] !== undefined
        ? tzOffsets[spoofedTimezone]
        : 300;

  const origResolvedOptions =
    Intl.DateTimeFormat.prototype.resolvedOptions;
  Intl.DateTimeFormat.prototype.resolvedOptions = function () {
    const result = origResolvedOptions.call(this);
    result.timeZone = spoofedTimezone;
    return result;
  };

  Date.prototype.getTimezoneOffset = function () {
    return spoofedOffset;
  };

  // --- Navigator Permissions ---
  if (navigator.permissions) {
    const origQuery = navigator.permissions.query;
    navigator.permissions.query = function (desc) {
      const permDefaults = {
        notifications: "prompt",
        geolocation: "prompt",
        camera: "prompt",
        microphone: "prompt",
        "persistent-storage": "granted",
        "background-sync": "granted",
      };
      const name = desc && desc.name;
      if (name && name in permDefaults) {
        return Promise.resolve({
          state: permDefaults[name],
          status: permDefaults[name],
          onchange: null,
          addEventListener: function () {},
          removeEventListener: function () {},
          dispatchEvent: function () {
            return true;
          },
        });
      }
      return origQuery.call(this, desc);
    };
  }

  // --- Client Hints (navigator.userAgentData) ---
  const uaData = CFG.user_agent_data || {
    brands: [
      { brand: "Chromium", version: "120" },
      { brand: "Google Chrome", version: "120" },
      { brand: "Not_A Brand", version: "8" },
    ],
    mobile: false,
    platform: "Linux",
  };

  if ("userAgentData" in navigator) {
    const origUAData = navigator.userAgentData;
    Object.defineProperty(navigator, "userAgentData", {
      get: () => ({
        brands: uaData.brands || origUAData.brands,
        mobile: uaData.mobile !== undefined ? uaData.mobile : false,
        platform: uaData.platform || origUAData.platform,
        getHighEntropyValues: function (hints) {
          return Promise.resolve({
            brands: uaData.brands || origUAData.brands,
            mobile: uaData.mobile !== undefined ? uaData.mobile : false,
            platform: uaData.platform || "Linux",
            platformVersion: uaData.platformVersion || "6.1.0",
            architecture: "x86",
            model: "",
            uaFullVersion:
              (uaData.brands && uaData.brands[0] && uaData.brands[0].version) ||
              "120.0.0.0",
          });
        },
        toJSON: function () {
          return {
            brands: uaData.brands,
            mobile: uaData.mobile !== undefined ? uaData.mobile : false,
            platform: uaData.platform || "Linux",
          };
        },
      }),
    });
  }

  // --- Plugins ---
  const fakePlugins = [
    {
      name: "Chrome PDF Plugin",
      description: "Portable Document Format",
      filename: "internal-pdf-viewer",
      length: 1,
      0: { type: "application/x-google-chrome-pdf", suffixes: "pdf", description: "Portable Document Format" },
      item: function (i) { return i === 0 ? this[0] : null; },
      namedItem: function (name) { return name === "application/x-google-chrome-pdf" ? this[0] : null; },
    },
    {
      name: "Chrome PDF Viewer",
      description: "Portable Document Format",
      filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai",
      length: 1,
      0: { type: "application/pdf", suffixes: "pdf", description: "Portable Document Format" },
      item: function (i) { return i === 0 ? this[0] : null; },
      namedItem: function (name) { return name === "application/pdf" ? this[0] : null; },
    },
  ];

  Object.defineProperty(navigator, "plugins", {
    get: () => {
      const arr = fakePlugins;
      arr.item = function (i) { return arr[i] || null; };
      arr.namedItem = function (name) { return arr.find((p) => p.name === name) || null; };
      arr.refresh = function () {};
      return arr;
    },
  });
})();
