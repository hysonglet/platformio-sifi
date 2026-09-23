#include "rtthread.h"
#include "bf0_hal.h"
#include "bf0_hal_pmu.h"
#include "drv_io.h"
#include "littlevgl2rtt.h"
#include "lvgl.h"

/*
 * LVGL 9 demo.
 *
 * 目标硬件：立创·黄山派 (SF32LB52-ULP) + LCD 屏幕板 ZC-A1D85W-010
 *           (1.85" AMOLED, CO5300AF-01)
 *
 * 该屏是 AMOLED，没有背光，所谓"屏幕不亮"即整块屏没有上电/没有点亮。
 * 屏幕板的供电与面板使能信号如下（22P FPC）：
 *   PA38 : VSYS   -> VSYS_1   (面板电源 IC 输入)
 *   PA26 : VSYS_1 -> VCC33
 *   PA30 : VSYS_1 -> HR3V3
 *   PA01 : 面板电源 IC 使能 (FPC 上标为 BL_PWM，本板以 GPIO 方式驱动)
 *   PA00 : 面板 RESET
 * 另外模组 3.3V 输出 VDD33_VOUT (内部 LDO3) 接到 FPC pin17 VDD_3V3，
 * 默认无输出，必须由软件打开，否则屏幕板 3.3V 域不上电。
 */
#define PANEL_PWR_EN_PIN    (1)     /* PA01 */
#define RAIL_VSYS1_PIN      (38)    /* PA38 */
#define RAIL_VCC33_PIN      (26)    /* PA26 */
#define RAIL_HR3V3_PIN      (30)    /* PA30 */

static void panel_power_on(void)
{
    /* 模组内部 3.3V LDO -> FPC pin17 VDD_3V3（屏幕板 3.3V 域） */
    HAL_PMU_ConfigPeriLdo(PMU_PERI_LDO3_3V3, true, true);

    /* 电源域切换脚 + 面板使能脚，先复用为 GPIO 再拉高 */
    HAL_PIN_Set(PAD_PA38, GPIO_A38, PIN_NOPULL, 1);
    HAL_PIN_Set(PAD_PA26, GPIO_A26, PIN_NOPULL, 1);
    HAL_PIN_Set(PAD_PA30, GPIO_A30, PIN_NOPULL, 1);
    HAL_PIN_Set(PAD_PA01, GPIO_A1,  PIN_NOPULL, 1);

    BSP_GPIO_Set(RAIL_VSYS1_PIN, 1, 1);
    BSP_GPIO_Set(RAIL_VCC33_PIN, 1, 1);
    BSP_GPIO_Set(RAIL_HR3V3_PIN, 1, 1);
    BSP_GPIO_Set(PANEL_PWR_EN_PIN, 1, 1);

    /* 等电源稳定后再初始化面板 */
    HAL_Delay_us(2000);

    rt_kprintf("[panel] power on, GPIO1 DOR=0x%08x DIR=0x%08x\n",
               (unsigned)hwp_gpio1->DOR, (unsigned)hwp_gpio1->DIR);
}

static void panel_set_brightness(uint8_t percent)
{
    rt_device_t lcd = rt_device_find("lcd");

    if (lcd == RT_NULL)
    {
        rt_kprintf("[panel] lcd device not found!\n");
        return;
    }
    rt_device_control(lcd, RTGRAPHIC_CTRL_SET_BRIGHTNESS, &percent);
    rt_kprintf("[panel] brightness set to %d%%\n", percent);
}

/* LVGL v9 log callback: (level, buffer) */
static void lv_log_print_cb(lv_log_level_t level, const char *buf)
{
    (void)level;
    rt_kprintf("%s", buf);
}

static void btn_event_cb(lv_event_t *e)
{
    if (lv_event_get_code(e) == LV_EVENT_CLICKED)
    {
        lv_obj_t *label = lv_obj_get_child(lv_event_get_target(e), 0);
        static uint8_t cnt = 0;
        lv_label_set_text_fmt(label, "Button: %d", ++cnt);
    }
}

static void demo_ui_init(void)
{
    lv_obj_t *scr = lv_screen_active();

    /* 用高对比配色，便于确认面板是否真的在显示 */
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0000FF), LV_PART_MAIN);

    /* welcome label, centered on screen */
    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "Hello LVGL 9!");
    lv_obj_set_style_text_color(title, lv_color_hex(0xFFFFFF), LV_PART_MAIN);
    lv_obj_center(title);

    /* a button with a counter, top-left */
    lv_obj_t *btn = lv_button_create(scr);
    lv_obj_set_pos(btn, 10, 10);
    lv_obj_set_size(btn, 140, 60);
    lv_obj_add_event_cb(btn, btn_event_cb, LV_EVENT_ALL, NULL);

    lv_obj_t *btn_label = lv_label_create(btn);
    lv_label_set_text(btn_label, "Click me");
    lv_obj_center(btn_label);
}

static void lvgl_version_dump(void)
{
    rt_kprintf("[LVGL] version: %d.%d.%d\n",
               LVGL_VERSION_MAJOR, LVGL_VERSION_MINOR, LVGL_VERSION_PATCH);
#if LV_USE_LOG
    rt_kprintf("[LVGL] log enabled, LV_LOG_LEVEL=%d\n", LV_LOG_LEVEL);
#else
    rt_kprintf("[LVGL] log DISABLED\n");
#endif
}

/*----------------------------------------------------------------------------
 * 每 1 秒打印一次的日志任务
 *----------------------------------------------------------------------------*/
#define LOG_TASK_STACK_SIZE   2048
#define LOG_TASK_PRIORITY     20      /* 低于 main(10) / lcd_task(12)，不打扰渲染 */
#define LOG_TASK_PERIOD_MS    1000

static void log_task_entry(void *parameter)
{
    uint32_t count = 0;

    while (1)
    {
        count++;
        /* rt_kprintf 一般不支持浮点，fps 取整输出 */
        rt_kprintf("[log] count=%d tick=%d fps=%d\n",
                   count, rt_tick_get(), (int)lv_get_fps());
        rt_thread_mdelay(LOG_TASK_PERIOD_MS);
    }
}

static void log_task_init(void)
{
    rt_thread_t tid = rt_thread_create("log1s",
                                       log_task_entry, RT_NULL,
                                       LOG_TASK_STACK_SIZE,
                                       LOG_TASK_PRIORITY,
                                       LOG_TASK_PERIOD_MS / 10);

    if (tid == RT_NULL)
    {
        rt_kprintf("[log] create 1s log task failed!\n");
        return;
    }

    rt_thread_startup(tid);
    rt_kprintf("[log] 1s log task started\n");
}

int main(void)
{
    rt_kprintf("LVGL demo start\n");

    lvgl_version_dump();
    /* LVGL 日志走 RT-Thread 控制台 */
    lv_log_register_print_cb(lv_log_print_cb);

    /* 必须先给屏幕板上电，再初始化 LCD/面板 */
    panel_power_on();

    if (littlevgl2rtt_init("lcd") != RT_EOK)
    {
        rt_kprintf("LVGL init failed!\n");
        return -1;
    }

    demo_ui_init();

    /* 面板初始化完成后把亮度拉到 100%，AMOLED 无背光，亮度即显示亮度 */
    panel_set_brightness(100);

    /* 起一个独立线程，每秒打印一次运行信息 */
    log_task_init();

    while (1)
    {
        uint32_t ms = lv_timer_handler();
        rt_thread_mdelay(ms);
    }

    return 0;
}
