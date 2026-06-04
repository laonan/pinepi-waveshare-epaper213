DIR_Config   = ./lib/Config
DIR_Driver	 = ./lib/Driver
DIR_GUI		 = ./lib/GUI
DIR_FONTS    = ./lib/Fonts
DIR_EPD		 = ./lib/EPD
# 1. 核心改动：把这里指向你的新目录 src
DIR_SRC      = ./src
DIR_BIN      = ./bin

# 2. 核心改动：把 ${DIR_Examples} 替换为 ${DIR_SRC}
OBJ_C = $(wildcard ${DIR_Driver}/*.c ${DIR_GUI}/*.c ${DIR_EPD}/*.c ${DIR_Config}/*.c ${DIR_SRC}/*.c ${DIR_FONTS}/*.c )
OBJ_O = $(patsubst %.c,${DIR_BIN}/%.o,$(notdir ${OBJ_C}))

TARGET = pinepi

# USELIB = USE_BCM2835_LIB
# USELIB = USE_WIRINGPI_LIB
USELIB = USE_LGPIO_LIB
# USELIB = USE_GPIOD_LIB

DEBUG = -D $(USELIB)

ifeq ($(USELIB), USE_BCM2835_LIB)
    LIB = -lbcm2835 -lm 
	OBJ_O := $(filter-out ${DIR_BIN}/RPI_gpiod.o ${DIR_BIN}/dev_hardware_SPI.o ${DIR_BIN}/dev_hardware_i2c.o, ${OBJ_O})
else ifeq ($(USELIB), USE_WIRINGPI_LIB)
    LIB = -lwiringPi -lm 
	OBJ_O := $(filter-out ${DIR_BIN}/RPI_gpiod.o ${DIR_BIN}/dev_hardware_SPI.o ${DIR_BIN}/dev_hardware_i2c.o, ${OBJ_O})
else ifeq ($(USELIB), USE_LGPIO_LIB)
	LIB += -llgpio -lm 
	OBJ_O := $(filter-out ${DIR_BIN}/RPI_gpiod.o ${DIR_BIN}/dev_hardware_SPI.o ${DIR_BIN}/dev_hardware_i2c.o, ${OBJ_O})
else ifeq ($(USELIB), USE_GPIOD_LIB)
    LIB = -lgpiod -lm
endif
LIB += -lpthread

CC = gcc
MSG = -g -O0 -Wall
CFLAGS += $(MSG) $(DEBUG)

${TARGET}:${OBJ_O}
	$(CC) $(CFLAGS) $(OBJ_O) -o $@ $(LIB) -lm -lpthread
    
$(shell mkdir -p $(DIR_BIN))

# 3. 核心改动：把原本编译 examples 的规则，精准对齐到 $(DIR_SRC)
${DIR_BIN}/%.o:$(DIR_SRC)/%.c
	$(CC) $(CFLAGS) -c  $< -o $@ -I $(DIR_Config) -I $(DIR_Driver)  -I $(DIR_EPD) -I $(DIR_GUI)
    
${DIR_BIN}/%.o:$(DIR_Driver)/%.c
	$(CC) $(CFLAGS) -c  $< -o $@ -I $(DIR_Config) $(DEBUG)

${DIR_BIN}/%.o:$(DIR_EPD)/%.c
	$(CC) $(CFLAGS) -c  $< -o $@ -I $(DIR_Config) $(DEBUG)
    
${DIR_BIN}/%.o:$(DIR_FONTS)/%.c 
	$(CC) $(CFLAGS) -c  $< -o $@ $(DEBUG)
	
${DIR_BIN}/%.o:$(DIR_GUI)/%.c
	$(CC) $(CFLAGS) -c  $< -o $@ -I $(DIR_Config) $(DEBUG)
	
${DIR_BIN}/%.o:$(DIR_Config)/%.c
	$(CC) $(CFLAGS) -c  $< -o $@ $(LIB)

	
clean :
	rm -f $(DIR_BIN)/*
	rm -f $(TARGET)
