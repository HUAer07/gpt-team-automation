// YesCaptcha 自动化配置 (已由系统自动同步注入)
const config = {
    clientKey: '7ec6a48c8cfe76f4f1b52c0b569cc078b43f1a0d136129',
    host: 'https://api.yescaptcha.com',
    autorun: true,

    // 通用行为
    isTextCaptcha: false,
    endTimes: '20',
    isAutoClickCheckBox: true,
    checkBoxClickDelayTime: '300',
    isOpenEndTimes: true,
    isOpenCloudflare: true,
    isOpenCloudflareTurnstileProtocol: true,
    isAutoSubmit: true,
    autoSubmitDelayTime: 100,
    autoSubmitDelayFloatRate: 0.1,

    // 网络重试设置
    network: {
        hcaptchaVerifyFailDelay: 1000,
        hcaptchaVerifyTry: 3,
        recaptchaVerifyFailDelay: 1000,
        recaptchaVerifyTry: 2,
        funcaptchaVerifyFailDelay: 1000,
        funcaptchaVerifyTry: 3
    },

    // FunCaptcha / Arkose 独立配置 (重点接管 Stripe 拼图)
    funcaptchaConfig: {
        isOpen: true,
        actionAfterRecSuccess: 'nothing',
        actionAfterOneRecFail: 'nothing',
        actionAfterRecFail: 'nothing',
        actionDelay: 1000,
        isAutoClickPrePage: true,
        isRealClick: false
    },

    // hCaptcha 独立配置
    hcaptchaConfig: {
        isPassDragChallenge: false,
        isAutoRefresh: true,
        isSwitchToEnglishContent: true,
        isPassMoveCanvasChallenge: false,
        canvasRecordDuration: 6000,
        isRealClick: false
    },

    // reCaptcha 独立配置
    recaptchaConfig: {
        isOpen: true,
        isUseNewScript: true,
        delayFor1X1: 3000,
        isAdaptInvisible: false,
        isOpenProtocol: false,
        V3TaskType: 'RecaptchaV3TaskProxylessM1S7',
        V2TaskType: 'RecaptchaV2TaskProxyless'
    },

    // AWS WAF
    awsCaptchaConfig: {
        isOpen: true
    },

    isHideKey: false
};

// 动态同步并持久化至 chrome.storage.local (即使 profile 缓存已存在也能立即生效)
try {
    if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
        chrome.storage.local.get(['config'], function (result) {
            let current = (result && result.config) ? result.config : {};
            current.clientKey = config.clientKey;
            current.host = config.host;
            current.autorun = true;
            current.isOpenCloudflare = true;
            current.isOpenCloudflareTurnstileProtocol = true;
            current.isAutoClickCheckBox = true;
            current.isAutoSubmit = true;
            current.funcaptchaConfig = Object.assign({}, current.funcaptchaConfig || {}, config.funcaptchaConfig, { isOpen: true });
            current.hcaptchaConfig = Object.assign({}, current.hcaptchaConfig || {}, config.hcaptchaConfig, { isOpen: true });
            current.recaptchaConfig = Object.assign({}, current.recaptchaConfig || {}, config.recaptchaConfig, { isOpen: true });
            chrome.storage.local.set({ config: current });
        });
    }
} catch (e) {}
