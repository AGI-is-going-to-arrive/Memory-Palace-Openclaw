import { beforeEach, describe, expect, it } from 'vitest';

import i18n from '../../i18n';
import {
  localizeObservabilityStatus,
  localizeObservabilityText,
} from './observabilityI18n';

describe('observabilityI18n', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('translates common diagnostic messages into chinese', async () => {
    await i18n.changeLanguage('zh-CN');

    expect(
      localizeObservabilityText(
        'Configured transport order: stdio -> sse.',
        i18n.t.bind(i18n)
      )
    ).toBe('已配置 transport 顺序：stdio -> sse。');

    expect(
      localizeObservabilityText(
        'search_memory probe returned 3 hit(s).',
        i18n.t.bind(i18n)
      )
    ).toBe('search_memory 探针返回了 3 条命中。');

    expect(localizeObservabilityStatus('warn', i18n.t.bind(i18n))).toBe('告警');
  });

  it('preserves english diagnostics in the english UI', () => {
    expect(
      localizeObservabilityText(
        'Transport health check passed over stdio.',
        i18n.t.bind(i18n)
      )
    ).toBe('Transport health check passed over stdio.');

    expect(
      localizeObservabilityText(
        'verify passed with 16 check(s).',
        i18n.t.bind(i18n)
      )
    ).toBe('verify passed with 16 check(s).');

    expect(localizeObservabilityStatus('pass', i18n.t.bind(i18n))).toBe('pass');
  });
});
