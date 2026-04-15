import { beforeEach, describe, expect, it } from 'vitest';

import i18n from '../../i18n';
import { localizeSetupText, localizeSetupTextList } from './setupI18n';

describe('setupI18n', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('translates patterned installer messages into english', () => {
    expect(
      localizeSetupText(
        'Profile D 所需模型配置不完整，当前已自动回退到 Profile B。',
        i18n.t.bind(i18n)
      )
    ).toBe(
      'Profile D is missing required model settings and was automatically downgraded to Profile B.'
    );

    expect(
      localizeSetupText(
        'dashboard 端口 5173 已被其他服务占用，未自动启动新的 dashboard 进程。 可改用 `--dashboard-port 15173`。',
        i18n.t.bind(i18n)
      )
    ).toBe(
      'Dashboard port 5173 is already in use, so a new dashboard process was not started. Try `--dashboard-port 15173`.'
    );
  });

  it('translates exact and patterned setup result items into chinese', async () => {
    await i18n.changeLanguage('zh-CN');

    expect(
      localizeSetupText(
        'Setup completed for mode=full, requested profile=B, effective profile=B.',
        i18n.t.bind(i18n)
      )
    ).toBe('Setup 已完成：mode=full，requested profile=B，effective profile=B。');

    expect(
      localizeSetupText(
        'created runtime venv at C:/tmp/runtime',
        i18n.t.bind(i18n)
      )
    ).toBe('已创建 runtime venv：C:/tmp/runtime');

    expect(
      localizeSetupTextList(
        [
          'ensured plugins.allow contains memory-palace',
          'Open dashboard: http://127.0.0.1:15173',
        ],
        i18n.t.bind(i18n)
      )
    ).toEqual([
      '已确保 `plugins.allow` 包含 memory-palace',
      '打开 dashboard：http://127.0.0.1:15173',
    ]);
  });
});
