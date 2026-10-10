// Ghidra POST-analysis labels/comments for the FNIRSI 2C53P APP image V1.14.
// Addresses are runtime addresses (file offset + 0xE000).
// Re-apply any time with:  tools/ghidra/import.sh -process
//@category 2C53P
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.data.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.*;

public class Labels2C53P extends GhidraScript {

    Address a(long x) { return toAddr(x); }

    void fn(long addr, String name, String comment) throws Exception {
        Address ad = a(addr & ~1L);
        if (getInstructionAt(ad) == null) disassemble(ad);
        Function f = getFunctionAt(ad);
        if (f == null) f = createFunction(ad, name);
        if (f != null && name != null) f.setName(name, SourceType.USER_DEFINED);
        if (comment != null) setPlateComment(ad, comment);
    }

    void lab(long addr, String name, String comment) throws Exception {
        Symbol s = createLabel(a(addr), name, true, SourceType.USER_DEFINED);
        if (s != null) s.setPrimary();
        if (comment != null) setEOLComment(a(addr), comment);
    }

    @Override
    public void run() throws Exception {
        fn(0xE1BC, "Reset_Handler", null);
        fn(0xE1C4, "NMI_Handler", null);
        fn(0xE1C6, "HardFault_Handler", null);

        // --- FatFs (identified by call pattern in save/load; arg order matches FatFs R0.1x) ---
        fn(0x37084, "f_open", "FatFs f_open(FIL*, path, mode). mode 1=READ, 0x0A=WRITE|CREATE_ALWAYS");
        fn(0x37470, "f_read", "FatFs f_read(FIL*, buf, btr, UINT* br)");
        fn(0x37D08, "f_write", "FatFs f_write(FIL*, buf, btw, UINT* bw)");
        fn(0x36118, "f_lseek", "FatFs f_lseek(FIL*, ofs)");
        fn(0x35F58, "f_close", "FatFs f_close(FIL*)");
        fn(0x378D8, "f_unlink_maybe", "called with sprintf'd path in file-browser delete paths (unverified)");
        fn(0x418A4, "sprintf", "sprintf(dst, fmt, ...) - libc");
        fn(0x439F8, "malloc_maybe", "returns buffer for size arg; used by BMP writer with 0x1000");

        // --- waveform save/load ---
        fn(0x3F6C8, "save_simple_wave_bin",
           "Writes P:/Screenshot simple file/<n>.bin, n = *(u8*)0x20000223.\n" +
           "  f_write(g_simple_wave @0x200019A9, 926 bytes)\n" +
           "  then appends 1 byte: g_ch_mask (0x2000013B), forced to 4 if *(u8*)0x2000013A == 1.\n" +
           "File = 927 bytes: CH1 trace[461] + 2 B, CH2 trace[461] + 2 B, mask byte.\n" +
           "Trace values are display pixels (0..240, y_screen = 240 - v), NOT raw ADC; no timebase/V/div stored.");
        fn(0x3C510, "load_simple_wave_bin",
           "load_simple_wave_bin(int n): f_open READ, f_read 926 B into g_simple_wave, seek fsize-1, read mask byte.");
        fn(0x3F764, "save_screenshot_bmp",
           "Writes P:/Screenshot file/<n>.bmp: 54-byte BMP header, 480x272, 16 bpp, from framebuffer *(u32*)0x2000006C.");

        // --- SWM341 SDK driver functions (matched by register usage vs SWM341_Lib) ---
        fn(0xEEAC, "DMA_CH_Config", "SWM341 DMA_CH_Config(ch, DMA_InitStructure*): DMA @0x40000800, ch regs @+0x40*ch; enables IRQ4 when initStruct->IntEn");
        fn(0x11550, "GPIO_Init", "SWM341 GPIO_Init(GPIOx, pin, dir, pull_up, pull_down, open_drain)");
        fn(0x283E0, "PORT_Init", "SWM341 PORT_Init(PORTx, pin, func, digital_in)");
        fn(0x299F4, "TIMR_Init", "SWM341 TIMR_Init(TIMRx, mode, period, ...); TIMER block 0x40046800, 0x40/timer (TIMR4 = 0x40046900)");
        fn(0x29D7C, "TIMR_Start", "SWM341 timer start (+0x20 = reload?, +0x1C/+0x10 enable bits)");
        fn(0x2A1F0, "TIMR_Stop", "clears timer enable bit in 0x40046C40 / 0x40048C40");
        fn(0x35DB0, "fpga_dma_init", "DMA ch0: 519 x u32 from GPIOD IDR (0x40041830) -> g_fpga_frame, handshake DMA_EXHS_TIMR4, IRQ on; GPIOC[5] out; PORTC.4 = TIMR4_IN (func 5); GPIOD[0..7] in; TIMR4 external-count mode");
        fn(0xEFA8, "DMA_IRQHandler", "IRQ4: FPGA frame DMA complete -> TIMR_Stop(TIMR4), GPIO_SetBit(GPIOC,5)");
        fn(0x2E4FC, "uart0_send", "uart0_send(buf, len): sets 0x200002A8/AC/B0, enables UART0 TX int");
        fn(0x2E2A4, "uart2_send", "uart2_send(buf, len): sets 0x200002B8/BC/C0, enables UART2 TX int -> FPGA/DMM command frames");
        fn(0x2E14C, "dmm_frame_read", "copies 12-byte 5A A5 frame from 0x200002D2 when rx idx == 12");
        fn(0xF428, "dmm_parse_frame", "decodes BCD digits/range flags from the 12-byte DMM frame");
        lab(0x40046900L, "TIMR4", null); lab(0x200002D2L, "g_uart2_rx_buf", "12 B: 5A A5 DMM frame or AA 55 echo (10 B)");
        lab(0x200002C8L, "g_uart0_rx_buf", "10 B");
        fn(0x2F070, "USB_IRQHandler", "IRQ77 USB; uses 0x20000114 -> 0x2000194C (USB/MSC state)");
        fn(0x2162C, "LCD_IRQHandler", "IRQ73 LCDC 0x40002000");
        fn(0x2E0A0, "UART0_IRQHandler", "IRQ0 UART0 0x40042000 (921600 baud factory/jig channel per efim-sys/2C53P_LEP_patch)");
        fn(0x2E19C, "UART2_IRQHandler", "IRQ8 UART2 0x40043000");
        fn(0x11398, "GPIOB_IRQHandler", "IRQ36 GPIOB EXTI; writes flag 0x200019A8");
        fn(0x1142C, "GPIOC_IRQHandler", "IRQ37 GPIOC EXTI pin 8 = GT911 touch INT; reads I2C reg 0x814E");
        fn(0x15120, "uart0_poll_boot_packet", "Polls UART0 for 5-byte 'd' hi lo cksum LF packet (boot ID); patched by LEP patch");
        fn(0x1AF38, "boot_config_check", "checks flash 0xD028 top byte == 0xEB else 'Load encryption program!' + wait for UART0 packet");
        fn(0x1ABA4, "config_flash_write", "erases sector 0xD000, programs 80 B config");
        lab(0x2000014AL, "g_boot_id", "u16 from UART0 packet");
        lab(0x40000800L, "DMA", null); lab(0x40040000L, "GPIOA", null); lab(0x40040800L, "GPIOB", null);
        lab(0x40041000L, "GPIOC", null); lab(0x40041800L, "GPIOD", null); lab(0x40042000L, "UART0", null);
        lab(0x40042800L, "UART1", null); lab(0x40043000L, "UART2", null); lab(0x40044000L, "SPI0", null);
        lab(0x40046000L, "PWM", null); lab(0x40046800L, "TIMER", null); lab(0x40048800L, "BTIMER", null);
        lab(0x4004A000L, "FLASHC", null); lab(0x4004A800L, "SFC", null); lab(0x400A0000L, "PORT", null);
        lab(0x400AA000L, "ANALOGC", null); lab(0x40002000L, "LCDC", null); lab(0x40005000L, "USBD", null);
        lab(0x40000000L, "SYSCON", null); lab(0x88000000L, "SDRAMC", null);

        // --- FreeRTOS + protocol tasks ---
        fn(0x40C10, "xQueueGenericSend", "FreeRTOS xQueueGenericSend(q, item, ticks, pos)");
        fn(0x40F0C, "xQueueReceive", "FreeRTOS xQueueReceive(q, buf, ticks)");
        fn(0x40BA0, "xQueueReset_maybe", null);
        fn(0xE372, "memcmp_like", "compare n bytes (no return value used)");
        fn(0x23120, "uart0_rx_task", "reads UART0 frames (uart0_frame_read 0x2E2CC) and dispatches to uart0_cmd_responder");
        fn(0x23178, "uart0_cmd_responder", "GET cmds 1..0x13 -> g_settings fields, 0x1A boot id, 0x28..0x3B measurement bytes @0x20000190+cmd, 0x54/0x55 u16; SET cmds 3,4,5,6,0xF,0x10,0x12,0x13,0x21... write g_settings and call apply (0x16000). Reply [cmd][..data][cksum][0x0A]");
        fn(0x2E2CC, "uart0_frame_read", "copies 10-byte UART0 rx buf to caller when count != 0");
        fn(0x16000, "settings_apply", "settings_apply(what, ...): 1..6 = push FPGA commands for changed settings (verify)");
        lab(0x20000284L, "g_uart0_cmd_queue", "FreeRTOS queue of u16 items consumed by uart0_cmd_responder");
        lab(0x20000288L, "g_fpga_cmd_queue", "FreeRTOS queue of u16 {cmdH,cmdL} items -> 10-byte UART2 frames");
        lab(0x20000119L, "g_fpga_tx_frame", "10 B: [0][1] hdr, [2] cmdH, [3] cmdL, [4..8] params/0xAA, [9] cksum = cmdL+cmdH");
        lab(0x200002F2L, "g_uart0_tx_frame", "reply frame for UART0");
        lab(0x200002E8L, "g_uart0_rx_frame", "parsed request frame");

        fn(0xEE04, "backlight_set", "PWM reg 0x400460A8 = brightness(0x20000252) * 20; 0x3FE00 defaults it to 1000");
        lab(0x20000252L, "g_brightness", null);
        lab(0x2000012AL, "g_ch1_vdiv_idx", "g_settings+2 (UART0 cmd 3), default 6; paired with +3");
        lab(0x2000012BL, "g_ch2_vdiv_idx", "g_settings+3 (UART0 cmd 4), default 6");
        lab(0x20000129L, "g_timebase_idx_maybe", "g_settings+1 (UART0 cmd 2)");
        fn(0x28BA4, "config_save_and_reboot", "packs 20 words (settings, positions, boot id, brightness, 0xEB marker @+0x28, calibration 0x1B8..0x1CB) -> FLASH_Erase/Write @0xD000, then spins for WDT reset");
        fn(0x103C0, "FLASH_Erase", "sector erase (arg = addr 0xD000)");
        fn(0x10408, "FLASH_Write", "FLASH_Write(addr, words, count)");
        fn(0x2A060, "TIMR_Start", "sets enable bit in 0x40046C40 (TIMR4 = bit 4)");
        fn(0xEF94, "DMA_CH_Open", "DMA_CH_Open(ch) - re-arms single-shot frame DMA; called before TIMR_Start(TIMR4) in wave_draw_main");
        fn(0x116C0, "GPIO_ClrBit", null); fn(0x11534, "GPIO_SetBit", null); fn(0x11544, "GPIO_GetBit", null);
        lab(0x200001B8L, "g_cal_words", "calibration u32[4..5], defaults 0x80808080; saved at 0xD030; readable via UART0 GET 0x28..0x3B");
        fn(0x28F38, "app_init", "memset(g_settings,0,0x138); defaults; peripheral init; sends all 0x55xx setting notifications on UART0");

        // --- RAM ---
        if (getDataAt(a(0x200019A9L)) == null)
            createData(a(0x200019A9L), new ArrayDataType(ByteDataType.dataType, 926, 1));
        lab(0x200019A9L, "g_simple_wave", "u8[926]: CH1 px[461], CH1 b461,b462, CH2 px[461] @+463, CH2 b924,b925");
        lab(0x20001D48L, "g_fpga_frame", "capture frame from FPGA: u32 magic 0xAA @+0,+4; u32 samples @+8 (461 for CH1, low byte = 8-bit ADC code); (start,len) @+0x804/+0x80C; u32 magic 0x55 @+0x814,+0x818; total 0x81C");
        lab(0x20001D50L, "g_fpga_ch1_raw", "u32[461]: low byte = ADC code; trace px = (code & 0xFF) - 8, clamped 0..240");
        lab(0x20001B78L, "g_wave_ch2", "u8[461]: CH2 trace px = g_simple_wave + 463");
        lab(0x200001D0L, "g_wave_valid_start", "u16: first valid sample index (<=460)");
        lab(0x200001D2L, "g_wave_valid_len", "u16: valid sample count (start+len <= 461); stored BE after each trace in SDRAM slots");
        lab(0x80080000L, "fb_lcd", "480x272 RGB565 LCD framebuffer (*g_framebuffer_ptr)");
        fn(0x2F170, "wave_draw_main", "Main waveform render: converts g_fpga_frame -> g_simple_wave/g_wave_ch2 pixels (code-8, clamp 240), plots into SDRAM wave area, measurements.");
        fn(0x239EC, "wave_copy_to_slot", "Copies g_simple_wave/g_wave_ch2 (+start/len u16 BE) into SDRAM slot [DAT_2000194A]*0x3A2 (930 B per slot).");
        lab(0x20000128L, "g_settings", "+4 ch1 pos px, +5 ch2 pos px, +0x13 ch_mask (b0=CH1,b1=CH2), +0xFB save index");
        lab(0x2000012CL, "g_ch1_pos", null);
        lab(0x2000012DL, "g_ch2_pos", null);
        lab(0x2000013BL, "g_ch_mask", "bit0 CH1 on, bit1 CH2 on; ==3 enables math (ch1+ch2)");
        lab(0x20000223L, "g_simple_save_index", "%d in Screenshot simple file/%d.bin");
        lab(0x2000006CL, "g_framebuffer_ptr", "u32: 480x272 RGB565 screen framebuffer");
        lab(0x80700000L, "fb_wave_area", "u16 RGB565, stride 461 px; pixel(x,y) = fb[(240-v)*461 + x]");

        // --- strings ---
        lab(0x589B0L, "s_simple_bin_fmt", null);
        lab(0x31C64L, "s_simple_bin_fmt_2", null);
        lab(0x58B39L, "s_screenshot_bmp_fmt", null);

        println("Labels2C53P done; g_simple_wave=" + getSymbolAt(a(0x200019A9L)));
    }
}
